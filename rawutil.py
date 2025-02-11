import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from glob import glob

import duckdb
import pyarrow.parquet as pq
from duckdb import CatalogException


def initdb():
    """
    Initialize an in memory db instance and configure with our custom sql.
    """
    con = duckdb.connect(database=":memory:")
    run_sql_no_args(con,"initdb.sql")
    return con


def get_glob_paths_for_dataset(dataset, subdir="raw_sensor"):
    """
    Build fully-qualifed glob paths for the dataset path. Return a map keyed by top level (event) dir.
    Expected structure is one of:

    Multiple files with/without Hive structure:
    {dataset}/{eventType}/[{attr=value}/..]/{filename}.parquet

    Single file at the top level:
    {dataset}/{eventType}.parquet
    """
    dataset_path = dataset + "/" + subdir
    event_types = os.listdir(dataset_path)
    globs = defaultdict(set)
    for event_type in event_types:
        cur_event = dataset_path + "/" + event_type
        if os.path.isdir(cur_event):
            for dirpath, dirnames, filenames in os.walk(cur_event):
                if not dirnames:
                    if dirpath == cur_event:
                        # No dir globs needed
                        globs[event_type].add(f"{cur_event}/*.parquet")
                    else:
                        # Remove the pre-fix, including event_type. Convert that to a glob.
                        subdir = dirpath.replace(cur_event, "")
                        glob = "/".join(["*"] * subdir.count(os.path.sep))
                        globs[event_type].add(f"{cur_event}/{glob}/*.parquet")
                        logging.debug(f"{event_type} {subdir} {glob} has 0 subdirectories and {len(filenames)} files")
        else:
            # Treat as a simple, single file.
            if event_type.lower().endswith("parquet"):
                event = re.split(r"\.", event_type)[0]
                logging.info(f"{datetime.now()} Found {event} file: {event_type}")
                globs[event].add(cur_event)
    return validate_globs(globs)


def validate_globs(raw_data):
    """
    raw_data is a map of event_type -> list of globs.
    Confirm that:
    * There is only 1 glob per event_type. A poorly formed dataset dir structure could result in multiples.
    * That there are actually files in the path. It's not uncommon for collection of specific feature type to by inconsistent over time.
    * For each file, confirm that it has data. Wintap does produce empty parquet files and that confuses duckdb and some other tools.
        * If an empty file is found, delete it
        * When complete, if all files were empty, then remove the event_type/glob entry.
    """
    globs = {}
    for event_type, pathspec_set in raw_data.items():
        # Normally, we'll only have one pathspec.
        if len(pathspec_set) > 1:
            raise Exception(f"Too many leaf dirs!: {pathspec_set}")
        else:
            pathspec = next(iter(pathspec_set))
            num_files = len(glob(pathspec))
            if num_files == 0:
                logging.info(f"Not found: {pathspec}  Skipping")
            else:
                logging.info(f"Found {num_files} parquet files in {pathspec}")
                #                for file in glob(pathspec):
                #                    table=pq.read_table(file)
                #                    if table.num_rows==0:
                #                        logging.info(f'{file} is empty, deleting.')
                #                        os.remove(file)
                #                        num_files-=1
                if num_files == 0:
                    logging.info(f"Skipping empty glob: {pathspec}")
                else:
                    globs[event_type] = pathspec
    return globs


def get_globs_for(dataset, daypk):
    '''
    This function is intended to reduce the raw_sensor input to a single day of activity for processing.
    First, get the full glob paths for raw_sensor.  Then, splice in the dayPK specification. 
    Finally, confirm that specific path exists and has files.
    '''
    globs_all = get_glob_paths_for_dataset(dataset)
    globs = {}
    for event_type, pathspec in globs_all.items():
        # Add in the daypk filter
        pathspec = pathspec.replace("/*/", f"/dayPK={daypk}/", 1)
        num_files = len(glob(pathspec))
        if num_files == 0:
            logging.info(f"Not found: {pathspec}  Skipping")
        else:
            logging.info(f"Found {num_files} parquet files in {pathspec}")
            # Check for empty files. These confuse duckdb and lead to schema errors.
            for file in glob(pathspec):
                table = pq.read_table(file)
                if table.num_rows == 0:
                    logging.info(f"{file} is empty, deleting.")
            globs[event_type] = pathspec
    return globs


def loadSqlStatements(file):
    """
    Read sql script into map keyed by table name.
    """
    file = open(file, "r")
    lines = file.readlines()

    statements = {}
    count = 0
    # Strips the newline character
    curKey = ""
    curStatement = ""
    for count, line in enumerate(lines):
        if line.lower().startswith("create "):
            # For tables and views, use the object name
            curKey = line.strip().split()[-1]
            curStatement = line
        elif line.lower().startswith("update "):
            # Add line number to be sure its unique
            curKey = f"{line.strip()}-{count}"
            curStatement = line
        else:
            if line.strip() == ";":
                # We done. Save the statement. Don't save the semi-colon.
                statements[curKey] = curStatement
                curStatement = "SKIP"
            else:
                if curStatement != "SKIP":
                    curStatement += line
    return statements


def create_raw_views(con, raw_data, jpy):
    '''
    Create views in the db for each of the event_types.
    '''
    # Raw View Template
    raw_view_sql = """
    create view raw_{{event_type}} as
    select * from '{{filepath | sqlsafe}}'
    """
    for event_type, pathspec in raw_data.items():
        query, bind_params = jpy.prepare_query(
            raw_view_sql, {"event_type": event_type, "filepath": pathspec}
        )
        stmt = query % tuple(bind_params)
        logging.debug(f"Create raw view for {event_type} using {pathspec}")
        cursor = con.cursor()
        cursor.execute(stmt)
        cursor.close()

    # For now, processing REQUIREs that RAW_PROCESS_STOP exist even if its empty. Create an empty table if needed.
    create_empty_process_stop(con)


def create_empty_process_stop(con):
    """
    The current processing expects there to always be a RAW_PROCESS_STOP table for the final step for PROCESS.
    This function is used to create an empty table with the right structure for cases where there were no PROCESS_STOP events reported.
    """
    db_objects = con.execute(
        "select table_name, table_type from information_schema.tables where table_schema='main' and lower(table_name)='raw_process_stop'"
    ).fetchall()
    if len(db_objects) == 0:
        logging.info("Creating empty RAW_PROCESS_STOP")
        cursor = con.cursor()
        cursor.execute(
            """
        CREATE TABLE raw_process_stop (
            PidHash VARCHAR,
            ParentPidHash VARCHAR,
            CPUCycleCount BIGINT,
            CPUUtilization INTEGER,
            CommitCharge BIGINT,
            CommitPeak BIGINT,
            ReadOperationCount BIGINT,
            WriteOperationCount BIGINT,
            ReadTransferKiloBytes BIGINT,
            WriteTransferKiloBytes BIGINT,
            HardFaultCount INTEGER,
            TokenElevationType INTEGER,
            ExitCode BIGINT,
            MessageType VARCHAR,
            Hostname VARCHAR,
            ActivityType VARCHAR,
            EventTime BIGINT,
            ReceiveTime BIGINT,
            PID INTEGER,
            IncrType VARCHAR,
            EventCount INTEGER,
            FirstSeenMs BIGINT,
            LastSeenMs BIGINT
        )
        """
        )
        cursor.close()


def run_sql_no_args(con, sqlfile):
    '''
    Execute all SQL statements in the file without binding any parameters.
    Format should SQL delimited with semi-colons. Comments are allowed SQL 
    style:
       -- for single line
       /*
       Multi-line 
       */
    '''
    etl_sql = loadSqlStatements(sqlfile)
    for key in etl_sql:
        logging.info(f"Processing: {key}")
        try:
            con.execute(etl_sql[key])
        except CatalogException as e:
            logging.info(f"No raw data for {key}")
        except Exception as e:
            logging.info(f"  Failed: {e}")


def write_parquet(con, dataset, daypk=None):
    """
    Write tables/views from duckdb instance to parquet.
    Skip tables starting with 'raw_' or 'tmp'
    If daypk is provided, write to corresponding path in rolling.
    Otherwise, write to stdview.
    """
    db_objects = con.execute(
        "select table_name, table_type from information_schema.tables where table_schema='main' order by all"
    ).fetchall()
    for object_name, object_type in db_objects:
        if object_name.lower().startswith("raw_") or "tmp" in object_name.lower():
            logging.debug(f"Skipping {object_name}")
        else:
            logging.info(f"Writing {object_name}")
            if daypk == None:
                pathspec = f"{dataset}/stdview"
                filename = f"{object_name}.parquet"
            else:
                pathspec = f"{dataset}/rolling/{object_name}/dayPK={daypk}"
                filename = f"{object_name}-{daypk}.parquet"
            if not os.path.exists(pathspec):
                os.makedirs(pathspec)
                logging.debug("folder '{}' created ".format(pathspec))
            else:
                logging.debug("folder {} already exists".format(pathspec))
            # TODO Add test for file existence
            sql = f"COPY (SELECT * FROM {object_name}) TO '{pathspec}/{filename}' (FORMAT 'parquet')"
            con.execute(sql)
