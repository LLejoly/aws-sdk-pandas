"""Amazon Timestream Module."""

import itertools
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Literal, Optional, Union, cast, overload

import boto3
from botocore.config import Config

import awswrangler.pandas as pd
from awswrangler import _data_types, _utils, exceptions, s3
from awswrangler._config import apply_configs
from awswrangler._distributed import engine
from awswrangler._executor import _BaseExecutor, _get_executor
from awswrangler.distributed.ray import ray_get
from awswrangler.typing import TimestreamBatchLoadReportS3Configuration

if TYPE_CHECKING:
    from mypy_boto3_timestream_query.type_defs import PaginatorConfigTypeDef, QueryResponseTypeDef, RowTypeDef
    from mypy_boto3_timestream_write.client import TimestreamWriteClient

_BATCH_LOAD_FINAL_STATES: List[str] = ["SUCCEEDED", "FAILED", "PROGRESS_STOPPED", "PENDING_RESUME"]
_BATCH_LOAD_WAIT_POLLING_DELAY: float = 2  # SECONDS
_TIME_UNITS = ["MILLISECONDS", "SECONDS", "MICROSECONDS", "NANOSECONDS"]

_logger: logging.Logger = logging.getLogger(__name__)


def _df2list(df: pd.DataFrame) -> List[List[Any]]:
    """Extract Parameters."""
    parameters: List[List[Any]] = df.values.tolist()
    for i, row in enumerate(parameters):
        for j, value in enumerate(row):
            if pd.isna(value):
                parameters[i][j] = None
            elif hasattr(value, "to_pydatetime"):
                parameters[i][j] = value.to_pydatetime()
    return parameters


def _format_timestamp(timestamp: Union[int, datetime]) -> str:
    if isinstance(timestamp, int):
        return str(round(timestamp / 1_000_000))
    if isinstance(timestamp, datetime):
        return str(round(timestamp.timestamp() * 1_000))
    raise exceptions.InvalidArgumentType("`time_col` must be of type timestamp.")


def _format_measure(measure_name: str, measure_value: Any, measure_type: str) -> Dict[str, str]:
    return {
        "Name": measure_name,
        "Value": _format_timestamp(measure_value) if measure_type == "TIMESTAMP" else str(measure_value),
        "Type": measure_type,
    }


def _sanitize_common_attributes(
    common_attributes: Optional[Dict[str, Any]],
    version: int,
    measure_name: Optional[str],
) -> Dict[str, Any]:
    common_attributes = {} if not common_attributes else common_attributes
    # Values in common_attributes take precedence
    common_attributes.setdefault("Version", version)

    if "Time" not in common_attributes:
        # TimeUnit is MILLISECONDS by default for Timestream writes
        # But if a time_col is supplied (i.e. Time is not in common_attributes)
        # then TimeUnit must be set to MILLISECONDS explicitly
        common_attributes["TimeUnit"] = "MILLISECONDS"

    if "MeasureValue" in common_attributes and "MeasureValueType" not in common_attributes:
        raise exceptions.InvalidArgumentCombination(
            "MeasureValueType must be supplied alongside MeasureValue in common_attributes."
        )

    if measure_name:
        common_attributes.setdefault("MeasureName", measure_name)
    elif "MeasureName" not in common_attributes:
        raise exceptions.InvalidArgumentCombination(
            "MeasureName must be supplied with the `measure_name` argument or in common_attributes."
        )
    return common_attributes


@engine.dispatch_on_engine
def _write_batch(
    timestream_client: Optional["TimestreamWriteClient"],
    database: str,
    table: str,
    common_attributes: Dict[str, Any],
    cols_names: List[Optional[str]],
    measure_cols: List[Optional[str]],
    measure_types: List[str],
    dimensions_cols: List[Optional[str]],
    batch: List[Any],
) -> List[Dict[str, str]]:
    client_timestream = timestream_client if timestream_client else _utils.client(service_name="timestream-write")
    records: List[Dict[str, Any]] = []
    scalar = bool(len(measure_cols) == 1 and "MeasureValues" not in common_attributes)
    time_loc = 0
    measure_cols_loc = 1 if cols_names[0] else 0
    dimensions_cols_loc = 1 if len(measure_cols) == 1 else 1 + len(measure_cols)
    if all(cols_names):
        # Time and Measures are supplied in the data frame
        dimensions_cols_loc = 1 + len(measure_cols)
    elif all(v is None for v in cols_names[:2]):
        # Time and Measures are supplied in common_attributes
        dimensions_cols_loc = 0

    for row in batch:
        record: Dict[str, Any] = {}
        if "Time" not in common_attributes:
            record["Time"] = _format_timestamp(row[time_loc])
        if scalar and "MeasureValue" not in common_attributes:
            measure_value = row[measure_cols_loc]
            if pd.isnull(measure_value):
                continue
            record["MeasureValue"] = str(measure_value)
        elif not scalar and "MeasureValues" not in common_attributes:
            record["MeasureValues"] = [
                _format_measure(measure_name, measure_value, measure_value_type)  # type: ignore[arg-type]
                for measure_name, measure_value, measure_value_type in zip(
                    measure_cols, row[measure_cols_loc:dimensions_cols_loc], measure_types
                )
                if not pd.isnull(measure_value)
            ]
            if len(record["MeasureValues"]) == 0:
                continue
        if "MeasureValueType" not in common_attributes:
            record["MeasureValueType"] = measure_types[0] if scalar else "MULTI"
        # Dimensions can be specified in both common_attributes and the data frame
        dimensions = (
            [
                {"Name": name, "DimensionValueType": "VARCHAR", "Value": str(value)}
                for name, value in zip(dimensions_cols, row[dimensions_cols_loc:])
            ]
            if all(dimensions_cols)
            else []
        )
        if dimensions:
            record["Dimensions"] = dimensions
        if record:
            records.append(record)

    try:
        if records:
            _utils.try_it(
                f=client_timestream.write_records,
                ex=(
                    client_timestream.exceptions.ThrottlingException,
                    client_timestream.exceptions.InternalServerException,
                ),
                max_num_tries=5,
                DatabaseName=database,
                TableName=table,
                CommonAttributes=common_attributes,
                Records=records,
            )
    except client_timestream.exceptions.RejectedRecordsException as ex:
        return cast(List[Dict[str, str]], ex.response["RejectedRecords"])
    return []


@engine.dispatch_on_engine
def _write_df(
    df: pd.DataFrame,
    executor: _BaseExecutor,
    database: str,
    table: str,
    common_attributes: Dict[str, Any],
    cols_names: List[Optional[str]],
    measure_cols: List[Optional[str]],
    measure_types: List[str],
    dimensions_cols: List[Optional[str]],
    boto3_session: Optional[boto3.Session],
) -> List[Dict[str, str]]:
    timestream_client = _utils.client(
        service_name="timestream-write",
        session=boto3_session,
        botocore_config=Config(read_timeout=20, max_pool_connections=5000, retries={"max_attempts": 10}),
    )
    batches: List[List[Any]] = _utils.chunkify(lst=_df2list(df=df), max_length=100)
    _logger.debug("Writing %d batches of data", len(batches))
    return executor.map(
        _write_batch,  # type: ignore[arg-type]
        timestream_client,
        itertools.repeat(database),
        itertools.repeat(table),
        itertools.repeat(common_attributes),
        itertools.repeat(cols_names),
        itertools.repeat(measure_cols),
        itertools.repeat(measure_types),
        itertools.repeat(dimensions_cols),
        batches,
    )


def _cast_value(value: str, dtype: str) -> Any:  # pylint: disable=too-many-branches,too-many-return-statements
    if dtype == "VARCHAR":
        return value
    if dtype in ("INTEGER", "BIGINT"):
        return int(value)
    if dtype == "DOUBLE":
        return float(value)
    if dtype == "BOOLEAN":
        return value.lower() == "true"
    if dtype == "TIMESTAMP":
        return datetime.strptime(value[:-3], "%Y-%m-%d %H:%M:%S.%f")
    if dtype == "DATE":
        return datetime.strptime(value, "%Y-%m-%d").date()
    if dtype == "TIME":
        return datetime.strptime(value[:-3], "%H:%M:%S.%f").time()
    if dtype == "ARRAY":
        return str(value)
    raise ValueError(f"Not supported Amazon Timestream type: {dtype}")


def _process_row(schema: List[Dict[str, str]], row: "RowTypeDef") -> List[Any]:
    row_processed: List[Any] = []
    for col_schema, col in zip(schema, row["Data"]):
        if col.get("NullValue", False):
            row_processed.append(None)
        elif "ScalarValue" in col:
            row_processed.append(_cast_value(value=col["ScalarValue"], dtype=col_schema["type"]))
        elif "ArrayValue" in col:
            row_processed.append(_cast_value(value=col["ArrayValue"], dtype="ARRAY"))  # type: ignore[arg-type]
        else:
            raise ValueError(
                f"Query with non ScalarType/ArrayColumnInfo/NullValue for column {col_schema['name']}. "
                f"Expected {col_schema['type']} instead of {col}"
            )
    return row_processed


def _rows_to_df(
    rows: List[List[Any]], schema: List[Dict[str, str]], df_metadata: Optional[Dict[str, str]] = None
) -> pd.DataFrame:
    df = pd.DataFrame(data=rows, columns=[c["name"] for c in schema])
    if df_metadata:
        try:
            df.attrs = df_metadata
        except AttributeError as ex:
            # Modin does not support attribute assignment
            _logger.error(ex)
    for col in schema:
        if col["type"] == "VARCHAR":
            df[col["name"]] = df[col["name"]].astype("string")
    return df


def _process_schema(page: "QueryResponseTypeDef") -> List[Dict[str, str]]:
    schema: List[Dict[str, str]] = []
    for col in page["ColumnInfo"]:
        if "ScalarType" in col["Type"]:
            schema.append({"name": col["Name"], "type": col["Type"]["ScalarType"]})
        elif "ArrayColumnInfo" in col["Type"]:
            schema.append({"name": col["Name"], "type": col["Type"]["ArrayColumnInfo"]})
        else:
            raise ValueError(f"Query with non ScalarType or ArrayColumnInfo for column {col['Name']}: {col['Type']}")
    return schema


def _paginate_query(
    sql: str,
    chunked: bool,
    pagination_config: Optional["PaginatorConfigTypeDef"],
    boto3_session: Optional[boto3.Session] = None,
) -> Iterator[pd.DataFrame]:
    client = _utils.client(
        service_name="timestream-query",
        session=boto3_session,
        botocore_config=Config(read_timeout=60, retries={"max_attempts": 10}),
    )
    paginator = client.get_paginator("query")
    rows: List[List[Any]] = []
    schema: List[Dict[str, str]] = []
    page_iterator = paginator.paginate(QueryString=sql, PaginationConfig=pagination_config or {})
    for page in page_iterator:
        if not schema:
            schema = _process_schema(page=page)
            _logger.debug("schema: %s", schema)
        for row in page["Rows"]:
            rows.append(_process_row(schema=schema, row=row))
        if len(rows) > 0:
            df_metadata = {}
            if chunked:
                if "NextToken" in page:
                    df_metadata["NextToken"] = page["NextToken"]
                df_metadata["QueryId"] = page["QueryId"]

            yield _rows_to_df(rows, schema, df_metadata)
        rows = []


@_utils.validate_distributed_kwargs(
    unsupported_kwargs=["boto3_session"],
)
def write(
    df: pd.DataFrame,
    database: str,
    table: str,
    time_col: Optional[str] = None,
    measure_col: Union[str, List[Optional[str]], None] = None,
    dimensions_cols: Optional[List[Optional[str]]] = None,
    version: int = 1,
    use_threads: Union[bool, int] = True,
    measure_name: Optional[str] = None,
    common_attributes: Optional[Dict[str, Any]] = None,
    boto3_session: Optional[boto3.Session] = None,
) -> List[Dict[str, str]]:
    """Store a Pandas DataFrame into an Amazon Timestream table.

    Note
    ----
    In case `use_threads=True`, the number of threads from os.cpu_count() is used.

    If the Timestream service rejects a record(s),
    this function will not throw a Python exception.
    Instead it will return the rejection information.

    Note
    ----
    If ``time_col`` column is supplied, it must be of type timestamp. ``TimeUnit`` is set to MILLISECONDS by default.

    Parameters
    ----------
    df : pandas.DataFrame
        Pandas DataFrame https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.html
    database : str
        Amazon Timestream database name.
    table : str
        Amazon Timestream table name.
    time_col : Optional[str]
        DataFrame column name to be used as time. MUST be a timestamp column.
    measure_col : Union[str, List[str], None]
        DataFrame column name(s) to be used as measure.
    dimensions_cols : Optional[List[str]]
        List of DataFrame column names to be used as dimensions.
    version : int
        Version number used for upserts.
        Documentation https://docs.aws.amazon.com/timestream/latest/developerguide/API_WriteRecords.html.
    use_threads : bool, int
        True to enable concurrent writing, False to disable multiple threads.
        If enabled, os.cpu_count() is used as the number of threads.
        If integer is provided, specified number is used.
    measure_name : Optional[str]
        Name that represents the data attribute of the time series.
        Overrides ``measure_col`` if specified.
    common_attributes : Optional[Dict[str, Any]]
        Dictionary of attributes shared across all records in the request.
        Using common attributes can optimize the cost of writes by reducing the size of request payloads.
        Values in ``common_attributes`` take precedence over all other arguments and data frame values.
        Dimension attributes are merged with attributes in record objects.
        Example: ``{"Dimensions": [{"Name": "device_id", "Value": "12345"}], "MeasureValueType": "DOUBLE"}``.
    boto3_session : boto3.Session(), optional
        Boto3 Session. If None, the default boto3 Session is used.

    Returns
    -------
    List[Dict[str, str]]
        Rejected records.
        Possible reasons for rejection are described here:
        https://docs.aws.amazon.com/timestream/latest/developerguide/API_RejectedRecord.html

    Examples
    --------
    Store a Pandas DataFrame into a Amazon Timestream table.

    >>> import awswrangler as wr
    >>> import pandas as pd
    >>> df = pd.DataFrame(
    >>>     {
    >>>         "time": [datetime.now(), datetime.now(), datetime.now()],
    >>>         "dim0": ["foo", "boo", "bar"],
    >>>         "dim1": [1, 2, 3],
    >>>         "measure": [1.0, 1.1, 1.2],
    >>>     }
    >>> )
    >>> rejected_records = wr.timestream.write(
    >>>     df=df,
    >>>     database="sampleDB",
    >>>     table="sampleTable",
    >>>     time_col="time",
    >>>     measure_col="measure",
    >>>     dimensions_cols=["dim0", "dim1"],
    >>> )
    >>> assert len(rejected_records) == 0

    Return value if some records are rejected.

    >>> [
    >>>     {
    >>>         'ExistingVersion': 2,
    >>>         'Reason': 'The record version 1 is lower than the existing version 2. A '
    >>>                   'higher version is required to update the measure value.',
    >>>         'RecordIndex': 0
    >>>     }
    >>> ]

    """
    measure_cols = measure_col if isinstance(measure_col, list) else [measure_col]
    measure_types: List[str] = (
        _data_types.timestream_type_from_pandas(df.loc[:, measure_cols]) if all(measure_cols) else []
    )
    dimensions_cols = dimensions_cols if dimensions_cols else [dimensions_cols]  # type: ignore[list-item]
    cols_names: List[Optional[str]] = [time_col] + measure_cols + dimensions_cols
    measure_name = measure_name if measure_name else measure_cols[0]
    common_attributes = _sanitize_common_attributes(common_attributes, version, measure_name)

    _logger.debug(
        "Writing to Timestream table %s in database %s\ncommon_attributes: %s\n, cols_names: %s\n, measure_types: %s",
        table,
        database,
        common_attributes,
        cols_names,
        measure_types,
    )

    # User can supply arguments in one of two ways:
    # 1. With the `common_attributes` dictionary which takes precedence
    # 2. With data frame columns
    # However, the data frame cannot be completely empty.
    # So if all values in `cols_names` are None, an exception is raised.
    if any(cols_names):
        dfs = _utils.split_pandas_frame(
            df.loc[:, [c for c in cols_names if c]], _utils.ensure_cpu_count(use_threads=use_threads)
        )
    else:
        raise exceptions.InvalidArgumentCombination(
            "At least one of `time_col`, `measure_col` or `dimensions_cols` must be specified."
        )
    _logger.debug("Writing %d dataframes to Timestream table", len(dfs))

    executor: _BaseExecutor = _get_executor(use_threads=use_threads)
    errors = list(
        itertools.chain(
            *ray_get(
                [
                    _write_df(
                        df=df,
                        executor=executor,
                        database=database,
                        table=table,
                        common_attributes=common_attributes,
                        cols_names=cols_names,
                        measure_cols=measure_cols,
                        measure_types=measure_types,
                        dimensions_cols=dimensions_cols,
                        boto3_session=boto3_session,
                    )
                    for df in dfs
                ]
            )
        )
    )
    return list(itertools.chain(*ray_get(errors)))


@apply_configs
def wait_batch_load_task(
    task_id: str,
    timestream_batch_load_wait_polling_delay: float = _BATCH_LOAD_WAIT_POLLING_DELAY,
    boto3_session: Optional[boto3.Session] = None,
) -> Dict[str, Any]:
    """
    Wait for the Timestream batch load task to complete.

    Parameters
    ----------
    task_id : str
        The ID of the batch load task.
    timestream_batch_load_wait_polling_delay : float, optional
        Time to wait between two polling attempts.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session is used if None.

    Returns
    -------
    Dict[str, Any]
        Dictionary with the describe_batch_load_task response.

    Examples
    --------
    >>> import awswrangler as wr
    >>> res = wr.timestream.wait_batch_load_task(task_id='task-id')

    Raises
    ------
    exceptions.TimestreamLoadError
        Error message raised by failed task.
    """
    timestream_client = _utils.client(service_name="timestream-write", session=boto3_session)

    response = timestream_client.describe_batch_load_task(TaskId=task_id)
    status = response["BatchLoadTaskDescription"]["TaskStatus"]
    while status not in _BATCH_LOAD_FINAL_STATES:
        time.sleep(timestream_batch_load_wait_polling_delay)
        response = timestream_client.describe_batch_load_task(TaskId=task_id)
        status = response["BatchLoadTaskDescription"]["TaskStatus"]
    _logger.debug("Task status: %s", status)
    if status != "SUCCEEDED":
        _logger.debug("Task response: %s", response)
        raise exceptions.TimestreamLoadError(response.get("ErrorMessage"))
    return response  # type: ignore[return-value]


@apply_configs
@_utils.validate_distributed_kwargs(
    unsupported_kwargs=["boto3_session", "s3_additional_kwargs"],
)
def batch_load(
    df: pd.DataFrame,
    path: str,
    database: str,
    table: str,
    time_col: str,
    dimensions_cols: List[str],
    measure_cols: List[str],
    measure_name_col: str,
    report_s3_configuration: TimestreamBatchLoadReportS3Configuration,
    time_unit: Optional[str] = None,
    record_version: int = 1,
    timestream_batch_load_wait_polling_delay: float = _BATCH_LOAD_WAIT_POLLING_DELAY,
    keep_files: bool = False,
    use_threads: Union[bool, int] = True,
    boto3_session: Optional[boto3.Session] = None,
    s3_additional_kwargs: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Batch load a Pandas DataFrame into a Amazon Timestream table.

    Note
    ----
    The supplied column names (time, dimension, measure) MUST match those in the Timestream table.

    Note
    ----
    Only ``MultiMeasureMappings`` is supported.
    See https://docs.aws.amazon.com/timestream/latest/developerguide/batch-load-data-model-mappings.html

    Parameters
    ----------
    df : pandas.DataFrame
        Pandas DataFrame.
    path : str
        S3 prefix to write the data.
    database : str
        Amazon Timestream database name.
    table : str
        Amazon Timestream table name.
    time_col : str
        Column name with the time data. It must be a long data type that represents the time since the Unix epoch.
    dimensions_cols : List[str]
        List of column names with the dimensions data.
    measure_cols : List[str]
        List of column names with the measure data.
    measure_name_col : str
        Column name with the measure name.
    report_s3_configuration : TimestreamBatchLoadReportS3Configuration
        Dictionary of the configuration for the S3 bucket where the error report is stored.
        https://docs.aws.amazon.com/timestream/latest/developerguide/API_ReportS3Configuration.html
        Example: {"BucketName": 'error-report-bucket-name'}
    time_unit : str, optional
        Time unit for the time column. MILLISECONDS by default.
    record_version : int, optional
        Record version.
    timestream_batch_load_wait_polling_delay : float, optional
        Time to wait between two polling attempts.
    keep_files : bool, optional
        Whether to keep the files after the operation.
    use_threads : Union[bool, int], optional
        True to enable concurrent requests, False to disable multiple threads.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session is used if None.
    s3_additional_kwargs : Optional[Dict[str, str]]
        Forwarded to S3 botocore requests.

    Returns
    -------
    Dict[str, Any]
        A dictionary of the batch load task response.

    Examples
    --------
    >>> import awswrangler as wr

    >>> response = wr.timestream.batch_load(
    >>>     df=df,
    >>>     path='s3://bucket/path/',
    >>>     database='sample_db',
    >>>     table='sample_table',
    >>>     time_col='time',
    >>>     dimensions_cols=['region', 'location'],
    >>>     measure_cols=['memory_utilization', 'cpu_utilization'],
    >>>     report_s3_configuration={'BucketName': 'error-report-bucket-name'},
    >>> )
    """
    path = path if path.endswith("/") else f"{path}/"
    if s3.list_objects(path=path, boto3_session=boto3_session, s3_additional_kwargs=s3_additional_kwargs):
        raise exceptions.InvalidArgument(
            f"The received S3 path ({path}) is not empty. "
            "Please, provide a different path or use wr.s3.delete_objects() to clean up the current one."
        )
    columns = [time_col, *dimensions_cols, *measure_cols, measure_name_col]

    try:
        s3.to_csv(
            df=df.loc[:, columns],
            path=path,
            index=False,
            dataset=True,
            mode="append",
            use_threads=use_threads,
            boto3_session=boto3_session,
            s3_additional_kwargs=s3_additional_kwargs,
        )
        measure_types: List[str] = _data_types.timestream_type_from_pandas(df.loc[:, measure_cols])
        return batch_load_from_files(
            path=path,
            database=database,
            table=table,
            time_col=time_col,
            dimensions_cols=dimensions_cols,
            measure_cols=measure_cols,
            measure_types=measure_types,
            report_s3_configuration=report_s3_configuration,
            time_unit=time_unit,
            measure_name_col=measure_name_col,
            record_version=record_version,
            timestream_batch_load_wait_polling_delay=timestream_batch_load_wait_polling_delay,
            boto3_session=boto3_session,
        )
    finally:
        if not keep_files:
            _logger.debug("Deleting objects in S3 path: %s", path)
            s3.delete_objects(
                path=path,
                use_threads=use_threads,
                boto3_session=boto3_session,
                s3_additional_kwargs=s3_additional_kwargs,
            )


@apply_configs
def batch_load_from_files(
    path: str,
    database: str,
    table: str,
    time_col: str,
    dimensions_cols: List[str],
    measure_cols: List[str],
    measure_types: List[str],
    measure_name_col: str,
    report_s3_configuration: TimestreamBatchLoadReportS3Configuration,
    time_unit: Optional[str] = None,
    record_version: int = 1,
    data_source_csv_configuration: Optional[Dict[str, Union[str, bool]]] = None,
    timestream_batch_load_wait_polling_delay: float = _BATCH_LOAD_WAIT_POLLING_DELAY,
    boto3_session: Optional[boto3.Session] = None,
) -> Dict[str, Any]:
    """Batch load files from S3 into a Amazon Timestream table.

    Note
    ----
    The supplied column names (time, dimension, measure) MUST match those in the Timestream table.

    Note
    ----
    Only ``MultiMeasureMappings`` is supported.
    See https://docs.aws.amazon.com/timestream/latest/developerguide/batch-load-data-model-mappings.html

    Parameters
    ----------
    path : str
        S3 prefix to write the data.
    database : str
        Amazon Timestream database name.
    table : str
        Amazon Timestream table name.
    time_col : str
        Column name with the time data. It must be a long data type that represents the time since the Unix epoch.
    dimensions_cols : List[str]
        List of column names with the dimensions data.
    measure_cols : List[str]
        List of column names with the measure data.
    measure_name_col : str
        Column name with the measure name.
    report_s3_configuration : TimestreamBatchLoadReportS3Configuration
        Dictionary of the configuration for the S3 bucket where the error report is stored.
        https://docs.aws.amazon.com/timestream/latest/developerguide/API_ReportS3Configuration.html
        Example: {"BucketName": 'error-report-bucket-name'}
    time_unit : str, optional
        Time unit for the time column. MILLISECONDS by default.
    record_version : int, optional
        Record version.
    data_source_csv_configuration : Dict[str, Union[str, bool]], optional
        Dictionary of the data source CSV configuration.
        https://docs.aws.amazon.com/timestream/latest/developerguide/API_CsvConfiguration.html
    timestream_batch_load_wait_polling_delay : float, optional
        Time to wait between two polling attempts.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session is used if None.

    Returns
    -------
    Dict[str, Any]
        A dictionary of the batch load task response.

    Examples
    --------
    >>> import awswrangler as wr

    >>> response = wr.timestream.batch_load_from_files(
    >>>     path='s3://bucket/path/',
    >>>     database='sample_db',
    >>>     table='sample_table',
    >>>     time_col='time',
    >>>     dimensions_cols=['region', 'location'],
    >>>     measure_cols=['memory_utilization', 'cpu_utilization'],
    >>>     report_s3_configuration={'BucketName': 'error-report-bucket-name'},
    >>> )
    """
    timestream_client = _utils.client(service_name="timestream-write", session=boto3_session)
    bucket, prefix = _utils.parse_path(path=path)
    time_unit = time_unit if time_unit else "MILLISECONDS"
    if time_unit not in _TIME_UNITS:
        raise exceptions.InvalidArgument(f"Invalid time unit: {time_unit}. Must be one of {_TIME_UNITS}.")

    kwargs: Dict[str, Any] = {
        "TargetDatabaseName": database,
        "TargetTableName": table,
        "DataModelConfiguration": {
            "DataModel": {
                "TimeColumn": time_col,
                "TimeUnit": time_unit,
                "DimensionMappings": [{"SourceColumn": c} for c in dimensions_cols],
                "MeasureNameColumn": measure_name_col,
                "MultiMeasureMappings": {
                    "MultiMeasureAttributeMappings": [
                        {"SourceColumn": c, "MeasureValueType": t} for c, t in zip(measure_cols, measure_types)
                    ],
                },
            }
        },
        "DataSourceConfiguration": {
            "DataSourceS3Configuration": {"BucketName": bucket, "ObjectKeyPrefix": prefix},
            "DataFormat": "CSV",
            "CsvConfiguration": data_source_csv_configuration if data_source_csv_configuration else {},
        },
        "ReportConfiguration": {"ReportS3Configuration": report_s3_configuration},
        "RecordVersion": record_version,
    }

    task_id = timestream_client.create_batch_load_task(**kwargs)["TaskId"]
    return wait_batch_load_task(
        task_id=task_id,
        timestream_batch_load_wait_polling_delay=timestream_batch_load_wait_polling_delay,
        boto3_session=boto3_session,
    )


@overload
def query(
    sql: str,
    chunked: Literal[False] = ...,
    pagination_config: Optional[Dict[str, Any]] = ...,
    boto3_session: Optional[boto3.Session] = ...,
) -> pd.DataFrame:
    ...


@overload
def query(
    sql: str,
    chunked: Literal[True],
    pagination_config: Optional[Dict[str, Any]] = ...,
    boto3_session: Optional[boto3.Session] = ...,
) -> Iterator[pd.DataFrame]:
    ...


@overload
def query(
    sql: str,
    chunked: bool,
    pagination_config: Optional[Dict[str, Any]] = ...,
    boto3_session: Optional[boto3.Session] = ...,
) -> Union[pd.DataFrame, Iterator[pd.DataFrame]]:
    ...


def query(
    sql: str,
    chunked: bool = False,
    pagination_config: Optional[Dict[str, Any]] = None,
    boto3_session: Optional[boto3.Session] = None,
) -> Union[pd.DataFrame, Iterator[pd.DataFrame]]:
    """Run a query and retrieve the result as a Pandas DataFrame.

    Parameters
    ----------
    sql: str
        SQL query.
    chunked: bool
        If True returns DataFrame iterator, and a single DataFrame otherwise. False by default.
    pagination_config: Dict[str, Any], optional
        Pagination configuration dictionary of a form {'MaxItems': 10, 'PageSize': 10, 'StartingToken': '...'}
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 Session will be used if boto3_session receive None.

    Returns
    -------
    Union[pd.DataFrame, Iterator[pd.DataFrame]]
        Pandas DataFrame https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.html

    Examples
    --------
    Run a query and return the result as a Pandas DataFrame or an iterable.

    >>> import awswrangler as wr
    >>> df = wr.timestream.query('SELECT * FROM "sampleDB"."sampleTable" ORDER BY time DESC LIMIT 10')

    """
    result_iterator = _paginate_query(sql, chunked, cast("PaginatorConfigTypeDef", pagination_config), boto3_session)
    if chunked:
        return result_iterator

    # Prepending an empty DataFrame ensures returning an empty DataFrame if result_iterator is empty
    results = list(result_iterator)
    if len(results) > 0:
        # Modin's concat() can not concatenate empty data frames
        return pd.concat(results, ignore_index=True)
    return pd.DataFrame()


def create_database(
    database: str,
    kms_key_id: Optional[str] = None,
    tags: Optional[Dict[str, str]] = None,
    boto3_session: Optional[boto3.Session] = None,
) -> str:
    """Create a new Timestream database.

    Note
    ----
    If the KMS key is not specified, the database will be encrypted with a
    Timestream managed KMS key located in your account.

    Parameters
    ----------
    database: str
        Database name.
    kms_key_id: Optional[str]
        The KMS key for the database. If the KMS key is not specified,
        the database will be encrypted with a Timestream managed KMS key located in your account.
    tags: Optional[Dict[str, str]]
        Key/Value dict to put on the database.
        Tags enable you to categorize databases and/or tables, for example,
        by purpose, owner, or environment.
        e.g. {"foo": "boo", "bar": "xoo"})
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 Session will be used if boto3_session receive None.

    Returns
    -------
    str
        The Amazon Resource Name that uniquely identifies this database. (ARN)

    Examples
    --------
    Creating a database.

    >>> import awswrangler as wr
    >>> arn = wr.timestream.create_database("MyDatabase")

    """
    _logger.info("Creating Timestream database %s", database)
    client = _utils.client(service_name="timestream-write", session=boto3_session)
    args: Dict[str, Any] = {"DatabaseName": database}
    if kms_key_id is not None:
        args["KmsKeyId"] = kms_key_id
    if tags is not None:
        args["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]
    response = client.create_database(**args)
    return response["Database"]["Arn"]


def delete_database(
    database: str,
    boto3_session: Optional[boto3.Session] = None,
) -> None:
    """Delete a given Timestream database. This is an irreversible operation.

    After a database is deleted, the time series data from its tables cannot be recovered.

    All tables in the database must be deleted first, or a ValidationException error will be thrown.

    Due to the nature of distributed retries,
    the operation can return either success or a ResourceNotFoundException.
    Clients should consider them equivalent.

    Parameters
    ----------
    database: str
        Database name.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 Session will be used if boto3_session receive None.

    Returns
    -------
    None
        None.

    Examples
    --------
    Deleting a database

    >>> import awswrangler as wr
    >>> arn = wr.timestream.delete_database("MyDatabase")

    """
    _logger.info("Deleting Timestream database %s", database)
    client = _utils.client(service_name="timestream-write", session=boto3_session)
    client.delete_database(DatabaseName=database)


def create_table(
    database: str,
    table: str,
    memory_retention_hours: int,
    magnetic_retention_days: int,
    tags: Optional[Dict[str, str]] = None,
    timestream_additional_kwargs: Optional[Dict[str, Any]] = None,
    boto3_session: Optional[boto3.Session] = None,
) -> str:
    """Create a new Timestream database.

    Note
    ----
    If the KMS key is not specified, the database will be encrypted with a
    Timestream managed KMS key located in your account.

    Parameters
    ----------
    database: str
        Database name.
    table: str
        Table name.
    memory_retention_hours: int
        The duration for which data must be stored in the memory store.
    magnetic_retention_days: int
        The duration for which data must be stored in the magnetic store.
    tags: Optional[Dict[str, str]]
        Key/Value dict to put on the table.
        Tags enable you to categorize databases and/or tables, for example,
        by purpose, owner, or environment.
        e.g. {"foo": "boo", "bar": "xoo"})
    timestream_additional_kwargs : Optional[Dict[str, Any]]
        Forwarded to botocore requests.
        e.g. timestream_additional_kwargs={'MagneticStoreWriteProperties': {'EnableMagneticStoreWrites': True}}
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 Session will be used if boto3_session receive None.

    Returns
    -------
    str
        The Amazon Resource Name that uniquely identifies this database. (ARN)

    Examples
    --------
    Creating a table.

    >>> import awswrangler as wr
    >>> arn = wr.timestream.create_table(
    ...     database="MyDatabase",
    ...     table="MyTable",
    ...     memory_retention_hours=3,
    ...     magnetic_retention_days=7
    ... )

    """
    _logger.info("Creating Timestream table %s in database %s", table, database)
    client = _utils.client(service_name="timestream-write", session=boto3_session)
    timestream_additional_kwargs = {} if timestream_additional_kwargs is None else timestream_additional_kwargs
    args: Dict[str, Any] = {
        "DatabaseName": database,
        "TableName": table,
        "RetentionProperties": {
            "MemoryStoreRetentionPeriodInHours": memory_retention_hours,
            "MagneticStoreRetentionPeriodInDays": magnetic_retention_days,
        },
        **timestream_additional_kwargs,
    }
    if tags is not None:
        args["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]
    response = client.create_table(**args)
    return response["Table"]["Arn"]


def delete_table(
    database: str,
    table: str,
    boto3_session: Optional[boto3.Session] = None,
) -> None:
    """Delete a given Timestream table.

    This is an irreversible operation.

    After a Timestream database table is deleted, the time series data stored in the table cannot be recovered.

    Due to the nature of distributed retries,
    the operation can return either success or a ResourceNotFoundException.
    Clients should consider them equivalent.

    Parameters
    ----------
    database: str
        Database name.
    table: str
        Table name.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 Session will be used if boto3_session receive None.

    Returns
    -------
    None
        None.

    Examples
    --------
    Deleting a table

    >>> import awswrangler as wr
    >>> arn = wr.timestream.delete_table("MyDatabase", "MyTable")

    """
    _logger.info("Deleting Timestream table %s in database %s", table, database)
    client = _utils.client(service_name="timestream-write", session=boto3_session)
    client.delete_table(DatabaseName=database, TableName=table)


def list_databases(
    boto3_session: Optional[boto3.Session] = None,
) -> List[str]:
    """
    List all databases in timestream.

    Parameters
    ----------
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 Session will be used if boto3_session receive None.

    Returns
    -------
    List[str]
        a list of available timestream databases.

    Examples
    --------
    Querying the list of all available databases

    >>> import awswrangler as wr
    >>> wr.timestream.list_databases()
    ... ["database1", "database2"]


    """
    client = _utils.client(service_name="timestream-write", session=boto3_session)

    response = client.list_databases()
    dbs: List[str] = [db["DatabaseName"] for db in response["Databases"]]
    while "NextToken" in response:
        response = client.list_databases(NextToken=response["NextToken"])
        dbs += [db["DatabaseName"] for db in response["Databases"]]

    return dbs


def list_tables(database: Optional[str] = None, boto3_session: Optional[boto3.Session] = None) -> List[str]:
    """
    List tables in timestream.

    Parameters
    ----------
    database: str
        Database name. If None, all tables in Timestream will be returned. Otherwise, only the tables inside the
        given database are returned.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 Session will be used if boto3_session receive None.

    Returns
    -------
    List[str]
        A list of table names.

    Examples
    --------
    Listing all tables in timestream across databases

    >>> import awswrangler as wr
    >>> wr.timestream.list_tables()
    ... ["table1", "table2"]

    Listing all tables in timestream in a specific database

    >>> import awswrangler as wr
    >>> wr.timestream.list_tables(DatabaseName="database1")
    ... ["table1"]

    """
    client = _utils.client(service_name="timestream-write", session=boto3_session)
    args = {} if database is None else {"DatabaseName": database}
    response = client.list_tables(**args)  # type: ignore[arg-type]
    tables: List[str] = [tbl["TableName"] for tbl in response["Tables"]]
    while "nextToken" in response:
        response = client.list_tables(**args, NextToken=response["NextToken"])  # type: ignore[arg-type]
        tables += [tbl["TableName"] for tbl in response["Tables"]]

    return tables
