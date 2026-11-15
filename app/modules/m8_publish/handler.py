"""M8 入口：补批采信、滚动发布、窗口版本、对账与导出的 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import store, web

from . import export, service

router = APIRouter(tags=["m8 夜班发布"])

CSV_MEDIA = "text/csv; charset=utf-8"


class IngestSampleBody(BaseModel):
    sample: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=128)


class IngestCsvBody(BaseModel):
    csv: str = Field(min_length=1)
    label: str = Field(default="手动补批", max_length=128)
    source_name: str = Field(default="manual.csv", max_length=128)


class PublishBody(BaseModel):
    cutoff: str | None = Field(default=None, max_length=32)
    note: str = Field(default="", max_length=256)


def _download(body: str, filename: str) -> Response:
    return Response(
        content=body.encode("utf-8"),
        media_type=CSV_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@router.get("/publish", response_class=Response)
def publish_page(request: Request):
    conn = store.session()
    try:
        ctx = {
            "batches": service.batches_view(conn),
            "publishes": service.publishes_view(conn),
            "current": service.current_view(conn),
            "reconcile": service.reconcile_view(conn),
            "exports": export.export_summary(conn),
        }
    finally:
        conn.close()
    return web.render(request, "publish.html", "publish", **ctx)


# --------------------------------------------------------------------------- #
# 补批
# --------------------------------------------------------------------------- #
@router.post("/api/publish/batches/ingest-sample")
def ingest_sample(body: IngestSampleBody):
    conn = store.session()
    try:
        return service.ingest_night_sample(conn, body.sample, label=body.label)
    finally:
        conn.close()


@router.post("/api/publish/batches/ingest-csv")
def ingest_csv(body: IngestCsvBody):
    conn = store.session()
    try:
        return service.ingest_csv(conn, body.csv, label=body.label, source_name=body.source_name)
    finally:
        conn.close()


@router.get("/api/publish/batches")
def list_batches():
    conn = store.session()
    try:
        return service.batches_view(conn)
    finally:
        conn.close()


@router.get("/api/publish/batches/{batch_id}")
def get_batch(batch_id: str):
    conn = store.session()
    try:
        return service.batch_detail(conn, batch_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 发布
# --------------------------------------------------------------------------- #
@router.post("/api/publish/publish")
def do_publish(body: PublishBody | None = None):
    body = body or PublishBody()
    conn = store.session()
    try:
        return service.publish(conn, cutoff=body.cutoff, note=body.note)
    finally:
        conn.close()


@router.get("/api/publish/publishes")
def list_publishes():
    conn = store.session()
    try:
        return service.publishes_view(conn)
    finally:
        conn.close()


@router.get("/api/publish/publishes/{publish_id}")
def get_publish(publish_id: str):
    conn = store.session()
    try:
        return service.publish_detail(conn, publish_id)
    finally:
        conn.close()


@router.get("/api/publish/current")
def current_windows():
    conn = store.session()
    try:
        return service.current_view(conn)
    finally:
        conn.close()


@router.get("/api/publish/windows/history")
def window_history(
    window_start: str = Query(min_length=10, max_length=32),
    sensor_id: str = Query(min_length=1, max_length=32),
):
    conn = store.session()
    try:
        return service.window_history(conn, window_start, sensor_id)
    finally:
        conn.close()


@router.get("/api/publish/evidence")
def evidence(
    sensor_id: str = Query(min_length=1, max_length=32),
    bucket: str = Query(min_length=10, max_length=32),
):
    conn = store.session()
    try:
        return service.evidence(conn, sensor_id=sensor_id, bucket=bucket)
    finally:
        conn.close()


@router.get("/api/publish/reconcile")
def reconcile():
    conn = store.session()
    try:
        return service.reconcile_view(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
@router.get("/api/publish/export/windows.csv")
def export_windows():
    conn = store.session()
    try:
        body = export.current_window_csv(conn)
    finally:
        conn.close()
    return _download(body, "published-windows.csv")


@router.get("/api/publish/export/samples.csv")
def export_samples():
    conn = store.session()
    try:
        body = export.samples_csv(conn)
    finally:
        conn.close()
    return _download(body, "published-samples.csv")


@router.get("/api/publish/export/raw.csv")
def export_raw():
    conn = store.session()
    try:
        body = export.raw_csv(conn)
    finally:
        conn.close()
    return _download(body, "published-raw.csv")


@router.get("/api/publish/export/manifest.json")
def export_manifest(publish_id: str | None = Query(default=None, max_length=64)):
    conn = store.session()
    try:
        body = export.publish_manifest_json(conn, publish_id)
    finally:
        conn.close()
    return Response(
        content=body.encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="publish-manifest.json"'},
    )
