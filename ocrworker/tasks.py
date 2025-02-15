import io
import logging
import uuid
import mimetypes
from pathlib import Path
from celery.app import default_app as celery_app

from celery import chain, group, shared_task

from ocrworker import config, db, plib, utils, s3, exceptions
from ocrworker.db.engine import Session
from ocrworker import constants as const
from ocrworker.ocr import run_one_page_ocr

logger = logging.getLogger(__name__)

settings = config.get_settings()

STARTED = "started"
COMPLETE = "complete"


@shared_task(
    name=const.WORKER_OCR_DOCUMENT,
    autoretry_for=(exceptions.S3DocumentNotFound,),
    # Wait for 10 seconds before starting each new try. At most retry 6 times.
    retry_kwargs={"max_retries": 6, "countdown": 10},
)
def ocr_document_task(document_id: str, lang: str):
    """OCR Document task with automatic retry

    This task may start before document being uploaded to S3.
    If document is not found on S3, `ocrworker.exceptions.S3DocumentNotFound`
    which causes task to be restarted after a delay of 10 seconds.
    """
    logger.debug(f"Task started, document_id={document_id}, lang={lang}")

    with Session() as db_session:
        doc_ver = db.get_last_version(db_session, doc_id=uuid.UUID(document_id))
        pages = db.get_pages(db_session, doc_ver_id=doc_ver.id)

    target_docver_uuid = uuid.uuid4()
    target_page_uuids = [uuid.uuid4() for _ in range(len(pages))]

    logger.debug(f"target_docver_uuid={target_docver_uuid}")
    logger.debug(f"target_page_uuids={target_page_uuids}")

    lang = lang.lower()

    doc_ver_path = plib.abs_docver_path(doc_ver.id, doc_ver.file_name)
    s3.download_docver(doc_ver.id, doc_ver.file_name)
    _type, _ = mimetypes.guess_type(doc_ver_path)

    if _type not in ("application/pdf", "application/image"):
        raise ValueError(f"Unsupported format for document: {doc_ver_path}")

    per_page_ocr_tasks = [
        ocr_page_task.s(
            doc_id=doc_ver.id,
            doc_ver_id=doc_ver.id,
            page_number=index + 1,
            target_docver_id=target_docver_uuid,
            target_page_id=target_page_uuid,
            lang=lang,
            preview_width=300,
        ).set(queue=prefixed(const.OCR))
        for index, target_page_uuid in enumerate(target_page_uuids)
    ]
    workflow = chain(
        group(per_page_ocr_tasks)
        | stitch_pages_task.s(
            doc_ver_id=doc_ver.id,
            target_docver_id=target_docver_uuid,
            target_page_ids=target_page_uuids,
        ).set(queue=prefixed(const.OCR))
        | update_db_task.s(
            doc_id=uuid.UUID(document_id),
            doc_ver_id=doc_ver.id,
            lang=lang,
            target_docver_id=target_docver_uuid,
            target_page_ids=target_page_uuids,
        ).set(queue=prefixed(const.OCR))
        | generate_preview.s(doc_id=document_id).set(queue=prefixed(const.OCR))
        | notify_index_task.s(doc_id=document_id).set(queue=prefixed(const.OCR))
    )
    # I've tried workflow.apply_async(queue=prefixed(OCR))
    # but not all tasks in the workflow reached OCR queue
    # See https://stackoverflow.com/questions/14953521/how-to-route-a-chain-of-tasks-to-a-specific-queue-in-celery  # noqa
    workflow.apply_async()


@shared_task()
def ocr_page_task(**kwargs):
    """OCR one single page"""
    logger.debug(f"Task started kwargs={kwargs}")

    doc_ver_id = kwargs["doc_ver_id"]
    target_page_id = kwargs["target_page_id"]
    lang = kwargs["lang"]
    page_number = kwargs["page_number"]
    preview_width = kwargs["preview_width"]

    with Session() as db_session:
        doc_ver = db.get_doc_ver(db_session, doc_ver_id)

    sidecar_dir = Path(
        settings.papermerge__main__media_root, const.OCR, const.PAGES
    )

    output_dir = plib.abs_page_path(target_page_id)

    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)

    if not sidecar_dir.parent.exists():
        sidecar_dir.parent.mkdir(parents=True, exist_ok=True)

    s3.download_docver(doc_ver.id, doc_ver.file_name)
    run_one_page_ocr(
        file_path=plib.abs_docver_path(doc_ver.id, doc_ver.file_name),
        output_dir=output_dir / const.PAGE_PDF,
        lang=lang,
        sidecar_dir=sidecar_dir,
        uuid=target_page_id,
        page_number=page_number,
        preview_width=preview_width,
    )
    # upload entire page dir (*.pdf file, *.svg, *.txt etc)
    s3.upload_page_dir(target_page_id)


@shared_task()
def stitch_pages_task(_, **kwargs):
    logger.debug(f"Stitching pages for args={kwargs}")

    doc_ver_id = kwargs["doc_ver_id"]
    target_docver_id = kwargs["target_docver_id"]
    target_page_ids = kwargs["target_page_ids"]
    with Session() as db_session:
        doc_ver = db.get_doc_ver(db_session, doc_ver_id)

    dst = plib.abs_docver_path(target_docver_id, doc_ver.file_name)
    srcs = [
        plib.abs_page_path(page_id) / const.PAGE_PDF
        for page_id in target_page_ids
    ]
    s3.download_pdf_pages(target_page_ids)
    utils.stitch_pdf(srcs=srcs, dst=dst)
    # same as dst, but relative
    s3.upload_file(plib.docver_path(target_docver_id, doc_ver.file_name))


@shared_task()
def update_db_task(_, **kwargs):
    logger.debug(f"Update DB kwargs={kwargs}")

    doc_id = kwargs["doc_id"]
    lang = kwargs["lang"]
    target_docver_id = kwargs["target_docver_id"]
    target_page_ids = kwargs["target_page_ids"]

    with Session() as db_session:
        db.increment_doc_ver(
            db_session,
            document_id=doc_id,
            target_docver_uuid=target_docver_id,
            target_page_uuids=[tid for tid in target_page_ids],
            lang=lang,
        )
        # these are newly created pages
        pages = db.get_pages(db_session, doc_ver_id=target_docver_id)
        streams = []
        for page in pages:
            abs_file_path = plib.abs_page_txt_path(page.id)
            s3.download_page_txt(page.id)
            if abs_file_path.exists():
                streams.append(open(abs_file_path))
            else:
                logger.debug(
                    f"{abs_file_path} not found. Page text set to empty string"
                )
                streams.append(io.StringIO(""))

        db.update_doc_ver_text(
            db_session, doc_ver_id=target_docver_id, streams=streams
        )


@shared_task()
def notify_index_task(_, **kwargs):
    logger.debug(f"Update notify index doc_id={kwargs}")

    doc_id = kwargs["doc_id"]

    celery_app.send_task(
        const.INDEX_ADD_DOCS,
        kwargs={"doc_ids": [doc_id]},
        route_name="i3",
    )


@shared_task()
def generate_preview(_, **kwargs):
    logger.debug(f"Generate thumbnail/page previews for doc_id={kwargs}")

    doc_id = kwargs["doc_id"]

    celery_app.send_task(
        const.S3_WORKER_GENERATE_PREVIEW,
        kwargs={"doc_id": doc_id},
        route_name="s3preview",
    )


def prefixed(name: str) -> str:
    pref = settings.papermerge__main__prefix
    if pref:
        return f"{pref}_{name}"

    return name
