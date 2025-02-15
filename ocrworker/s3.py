import uuid
import logging
import boto3
from botocore.client import Config
import asyncio
from httpx import AsyncClient

from pathlib import Path
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from ocrworker import config, plib
from ocrworker import exceptions
from ocrworker import constants as const

settings = config.get_settings()
logger = logging.getLogger(__name__)


def skip_if_s3_disabled(func):
    def inner(*args, **kwargs):
        if not is_enabled():
            logger.debug("S3 module is disabled")
            return

        return func(*args, **kwargs)

    return inner


def get_client() -> BaseClient:
    session = boto3.Session(
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region_name,
    )
    # https://stackoverflow.com/questions/26533245/the-authorization-mechanism-you-have-provided-is-not-supported-please-use-aws4  # noqa
    client = session.client("s3", config=Config(signature_version="s3v4"),endpoint_url=settings.s3_endpoint_url)

    return client


def is_enabled():
    s3_settings = [
        settings.papermerge__s3__bucket_name,
        settings.aws_access_key_id,
        settings.aws_secret_access_key,
    ]
    logger.debug(s3_settings)
    return all(s3_settings)


def obj_exists(keyname: str) -> bool:
    client = get_client()
    try:
        logger.debug(f"Checking of -{keyname}- objects exists")
        client.head_object(Bucket=get_bucket_name(), Key=keyname)
    except ClientError as ex:
        logger.debug(f"ClientError: {ex}")
        return False

    return True


@skip_if_s3_disabled
def download_docver(docver_id: uuid.UUID, file_name: str):
    """Downloads document version from S3"""
    doc_ver_path = plib.abs_docver_path(docver_id, file_name)
    keyname = Path(get_prefix()) / plib.docver_path(docver_id, file_name)
    if not doc_ver_path.exists():
        if not obj_exists(str(keyname)):
            # no local version + no s3 version
            raise exceptions.S3DocumentNotFound(f"S3 key {keyname} not found")

    client = get_client()
    doc_ver_path.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(get_bucket_name(), str(keyname), str(doc_ver_path))


@skip_if_s3_disabled
def upload_page_dir(page_id: uuid.UUID) -> None:
    """Uploads to S3 all content of the page folder

    If page was OCRed it will contain:
    - *.hocr
    - *.jpg
    - *.svg
    - *.pdf
    """
    page_dir = plib.abs_page_path(page_id).glob("*")
    for path in page_dir:
        if path.is_file():
            rel_file_path = plib.page_path(page_id) / path.name
            upload_file(rel_file_path)


@skip_if_s3_disabled
def upload_file(rel_file_path: Path):
    """Uploads to S3 file specified by relative path

    Path is relative to `media root`.
    E.g. path "thumbnails/jpg/bd/f8/bdf862be/100.jpg", means that
    file absolute path on the file system is:
        <media root>/thumbnails/jpg/bd/f8/bdf862be/100.jpg

    The S3 keyname will then be:
        <prefix>/thumbnails/jpg/bd/f8/bdf862be/100.jpg
    """
    s3_client = get_client()
    keyname = get_prefix() / rel_file_path
    target: Path = plib.rel2abs(rel_file_path)

    if not target.exists():
        logger.error(f"Target {target} does not exist. Upload to S3 canceled.")
        return

    if not target.is_file():
        logger.error(f"Target {target} is not a file. Upload to S3 canceled.")
        return

    logger.debug(f"target={target} keyname={keyname}")

    s3_client.upload_file(
        str(target), Bucket=get_bucket_name(), Key=str(keyname)
    )


@skip_if_s3_disabled
def download_page_txt(page_id: uuid.UUID):
    """Download document page txt from S3"""
    abs_path = plib.abs_page_txt_path(page_id)
    if abs_path.exists():
        return

    keyname = get_prefix() / plib.page_txt_path(page_id)
    if not obj_exists(str(keyname)):
        raise ValueError(f"{keyname} not found on S3")

    s3_client = get_client()
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    s3_client.download_file(get_bucket_name(), str(keyname), str(abs_path))


@skip_if_s3_disabled
def download_pdf_pages(target_page_ids: list[str]):
    """Downloads document pages from S3

    Will download only pages which are not found locally
    """
    to_download = []
    for page_id in target_page_ids:
        p = plib.abs_page_path(page_id) / const.PAGE_PDF
        if p.exists():
            logger.debug(f"{p} found locally")
        else:
            to_download.append(page_id)

    logger.debug(f"Queued for download from S3 {to_download}")
    download_many_pdf_pages(to_download)


def download_many_pdf_pages(page_ids: list[str]) -> int:
    return asyncio.run(supervisor(page_ids))


async def supervisor(page_ids: list[str]) -> int:
    async with AsyncClient() as client:
        to_download = [
            download_one_pdf_page(client, page_id) for page_id in page_ids
        ]

        res = await asyncio.gather(*to_download)

    return len(res)


async def download_one_pdf_page(client: AsyncClient, page_id: str):
    page_data = await get_pdf_page(client, page_id)
    file_path = plib.abs_page_path(page_id) / const.PAGE_PDF
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(page_data)


async def get_pdf_page(client: AsyncClient, page_id: str) -> bytes:
    s3_client = get_client()
    key = get_prefix() / plib.page_path(page_id) / const.PAGE_PDF
    request_url = s3_client.generate_presigned_url(
        "get_object",
        {"Bucket": get_bucket_name(), "Key": str(key)},
        ExpiresIn=30,
    )
    resp = await client.get(request_url, follow_redirects=True)
    return resp.read()


def get_bucket_name():
    return settings.papermerge__s3__bucket_name


def get_prefix():
    return settings.papermerge__main__prefix


def get_media_root():
    return settings.papermerge__main__media_root
