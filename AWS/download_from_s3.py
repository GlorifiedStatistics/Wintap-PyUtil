'''
Download raw Wintap data from an S3 bucket.

The S3 bucket is expected to be structured as follows:
  [prefix]/raw_sensor/[event_type]/uploadedDPK=YYYYMMDD/uploadedHPK=HH/[hive dirs]/[hostname=event_type-epoch].parquet

The local structure is:
  [localpath]/raw_sensor/[event_type]/dayPK=YYYYMMDD/hourPK=HH/[hostname=event_type-epoch].parquet

Note that the S3 structure is partitioned by day/hour uploaded, while the local path is partitoned by 
the day/hour the data was collected. Difference is notable when the agent has been offline for a period 
of time.
'''

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from functools import partial
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import boto3
import botocore
import tqdm

# Maximum number of open HTTP(s) connections
MAX_POOL_CONNECTIONS=50
# Maximum S3 download threads
MAX_WORKERS=32
# Maximum number of retries for failed downloads
MAX_RETRIES=3


S3File = NamedTuple(
    "S3File",
    [
        ("key", str),
        ("filename", str),
        ("s3_path", str),
        ("hostname", str),
        ("data_capture_ts", datetime),
        ("uploadedDPK", str),
        ("uploadedHPK", str),
        ("dataDPK", str),
        ("dataHPK", str),
        ("local_file_path", str),
        ("os", str),
        ("sensor_version", str),
        ("event_type", str),
    ],
)


def list_files(s3_client, bucket, prefix):
    """
    Lists all files, at any folder level, under the given prefix.
    No folders (CommonPrefixes) are returned as there is no delimiter given.
    """
    return _list_s3(s3_client, bucket, prefix, delimiter="")


def list_folders(s3_client, bucket, prefix):
    """
    Lists all folders (and files) at the given prefix level.
    Note that in practice, the wintap S3 organization doesn't mix files/folders at the same level.
    """
    return _list_s3(s3_client, bucket, prefix, delimiter="/")


def _list_s3(s3_client, bucket, prefix, delimiter="/"):
    """
    Get files/folders metadata from a specific S3 prefix.
    """
    file_names = []
    folders = []

    kwargs = {"Bucket": bucket, "Prefix": prefix, "Delimiter": delimiter}
    next_token = ""

    while next_token is not None:
        if next_token != "":
            kwargs["ContinuationToken"] = next_token

        response = s3_client.list_objects_v2(**kwargs)
        files = response.get("Contents")
        paths = response.get("CommonPrefixes")
        if paths != None:
            for path in paths:
                folders.append(path)

        if files != None:
            for file in files:
                file_names.append(file)

        next_token = response.get("NextContinuationToken")
    return file_names, folders

def download_one_file(bucket: str, client: boto3.client, s3_file: S3File):
    """
    Download a single file from S3
    Args:
        bucket (str): S3 bucket where images are hosted
        client (boto3.client): S3 client
        s3_file (S3File): S3 object metadata
    """
    make_dirs(s3_file)
    client.download_file(
        Bucket=bucket, Key=s3_file.key, Filename= os.path.join(s3_file.local_file_path, s3_file.filename)
    )

def download_files_threaded(bucket: str, client: boto3.client, s3_files, retry_attempt: int=0):
    '''
    Download files from S3 into the provided root path.
    Files are written to folders based on the timestamp they were collected, not uploaded.
    Multi-threaded, TQDM progress output.
    '''

    # The client is shared between threads
    func = partial(download_one_file, bucket, client)

    # List for storing possible failed downloads to retry later
    failed_downloads = []

    with tqdm.tqdm(desc="Downloading files from S3", total=len(s3_files)) as pbar:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Using a dict for preserving the downloaded file for each future, to store it as a failure if we need that
            futures = {
                executor.submit(func, s3_file): s3_file for s3_file in s3_files
            }
            for future in as_completed(futures):
                if future.exception():
                    failed_downloads.append(futures[future])
                pbar.update(1)
    if len(failed_downloads) > 0:
        if retry_attempt<MAX_RETRIES:
            logging.warn(f"  {len(failed_downloads)} downloads have failed. Retrying.")
            download_files_threaded(bucket,client,failed_downloads,retry_attempt+1)
        else:
            logging.warn(f"  {len(failed_downloads)} downloads have failed. Writing to CSV.")
            with open(
                os.path.join(".", f"failed_downloads_{datetime.now()}.csv"), "w", newline=""
            ) as csvfile:
                wr = csv.writer(csvfile, quoting=csv.QUOTE_ALL)
                wr.writerow(failed_downloads)


def download_files(bucket_name, s3_client, s3_files):
    """
    Download files from S3 into the provided root path.
    Files are written to folders based on the timestamp they were collected, not uploaded.
    Single-threaded, simple progress output.
    """
    count = 0
    for s3_file in s3_files:
        make_dirs(s3_file)
        download_one_file(
            bucket_name, s3_client, s3_file
        )
        count += 1
        if count % 1000 == 0:
            logging.info(f"      Downloaded: {count}")
    logging.info(f"    Downloaded: {count}")

def make_dirs(s3_file):
    if not os.path.exists(s3_file.local_file_path):
        os.makedirs(s3_file.local_file_path)
        logging.debug("folder '{}' created ".format(s3_file.local_file_path))


def hour_range(start_date, end_date):
    """
    Generate a timestamp for each hour in the range. These will correspond to the paths data is uploaded into.
    """
    for n in range(int((end_date - start_date).total_seconds() / 3600)):
        yield start_date + timedelta(hours=n)


def parse_s3_metadata(files, local_path, uploadedDPK, uploadedHPK, event_type):
    """
    Parse metadata from S3. This will be used for generating the correct path to write to.
    TODO: Write this data also to a parquet file for metadata analytics.
    """
    files_metadata = []
    for file in files:
        (s3_path, delim, filename) = file.get("Key").rpartition("/")
        hostname = filename.split("=")[0]
        data_capture_epoch = filename.split("=")[1].rsplit("-")[1].split(".")[0]
        data_capture_ts = datetime.fromtimestamp(int(data_capture_epoch), timezone.utc)
        datadpk = data_capture_ts.strftime("%Y%m%d")
        datahpk = data_capture_ts.strftime("%H")
        # Define fully-qualified local name
        local_file_path = f"{local_path}/raw_sensor/{event_type}/dayPK={datadpk}/hourPK={datahpk}"

        s3File = S3File(
            file.get("Key"),
            filename,
            s3_path,
            hostname,
            data_capture_ts,
            uploadedDPK,
            uploadedHPK,
            datadpk,
            datahpk,
            local_file_path,
            "windows",
            "v2",
            event_type,
        )
        files_metadata.append(s3File)
    return files_metadata


def main():
    parser = argparse.ArgumentParser(
        prog="downloadFromS3.py", description="Download Wintap files from S3"
    )
    parser.add_argument("--profile", help="AWS profile to use", required=True)
    parser.add_argument("-b", "--bucket", help="The S3 bucket", required=True)
    parser.add_argument("-p", "--prefix", help="S3 prefix within the bucket", required=True)
    parser.add_argument("-s", "--start", help="Start date (YYYYMMDD HH)", required=True)
    parser.add_argument("-e", "--end", help="End date (YYYYMMDD HH)", required=True)
    parser.add_argument("-l", "--localpath", help="Local path to write files", required=True)
    parser.add_argument('--log-level', default='INFO', help='Logging Level: INFO, WARN, ERROR, DEBUG')
    args = parser.parse_args()

    try:
        logging.basicConfig(level=args.log_level,format='%(asctime)s %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')
    except ValueError:
        logging.error('Invalid log level: {}'.format(args.log_level))
        sys.exit(1)

    session = boto3.Session(profile_name=args.profile)
    s3 = session.client("s3", config=botocore.client.Config(max_pool_connections=50))

    files, folders = list_folders(s3, bucket=args.bucket, prefix=args.prefix)

    # Top level is event types
    event_types = folders

    start_date = datetime.strptime(args.start, "%Y%m%d %H")
    end_date = datetime.strptime(args.end, "%Y%m%d %H")

    for event_type in event_types:
        logging.info(event_type.get("Prefix"))
        # Within an event type, iterate over date range by hour
        files_md=[]
        for single_date in hour_range(start_date, end_date):
            daypk = single_date.strftime("%Y%m%d")
            hourpk = single_date.strftime("%H")
            prefix = (
                f"{event_type.get('Prefix')}uploadedDPK={daypk}/uploadedHPK={hourpk}/"
            )

            files, folders = list_files(s3, bucket=args.bucket, prefix=prefix)
            if len(files) > 0 or len(folders) > 0:
                logging.debug(f"  {prefix}")
                logging.debug(f"    Files: {len(files)}  Folders: {len(folders)}")
                files_md.extend(parse_s3_metadata(
                    files,  args.localpath, daypk, hourpk, event_type.get("Prefix").split("/")[2]
                ))
            logging.info(f"  {prefix}  Files: {len(files)}  Folders: {len(folders)}  Total: {len(files_md)}")
        logging.info(f"   Downloading {len(files_md)}...")
        download_files_threaded(args.bucket, s3, files_md)


if __name__ == "__main__":
    main()
