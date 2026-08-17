import atexit
import logging
import os

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    app = Flask(__name__)
    # Surface app-level INFO logs (e.g. the detection startup status) on the
    # console when launched via run.ps1.
    app.logger.setLevel(logging.INFO)
    logging.getLogger("backend.services.image_service").setLevel(logging.INFO)
    # Local single-user tool: only the Vite dev origin needs cross-origin access.
    allowed = os.getenv("FRONTEND_ORIGIN", "http://127.0.0.1:5173,http://localhost:5173").split(",")
    CORS(app, resources={r"/api/*": {"origins": [o.strip() for o in allowed if o.strip()], "expose_headers": ["X-Export-Skipped-Count"]}})

    # Cap request bodies so oversized base64 image uploads can't exhaust
    # memory; edits carry one already-marked image inline.
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

    # One app-scoped ImageService so its requests.Session connection pool is
    # reused across tasks (the detached worker threads outlive each request).
    if "IMAGE_SERVICE" not in app.config:
        from .services.config_service import ConfigService
        from .services.image_service import ImageService

        app.config["IMAGE_SERVICE"] = ImageService(config_service=ConfigService())

        # Register graceful shutdown of the bounded thread pool executor.
        _img_service = app.config["IMAGE_SERVICE"]
        atexit.register(_img_service.shutdown, wait=False)

    @app.get("/api/health")
    def health_check():
        return jsonify({"ok": True, "service": "img-Creater-backend"})

    # ``<path:filename>`` is required because reference images live one level
    # deeper (``<history_id>/references/<file>``); the default converter cannot
    # match the ``/`` and every persisted reference URL would 404.
    @app.get("/api/results/<history_id>/<path:filename>")
    def result_file(history_id: str, filename: str):
        from .services.image_service import ImageService

        # history_id is user-controlled and joined into the served directory;
        # only accept ids that survive the same normalization used when the
        # session directory was created (rejects ``..`` and friends).
        if ImageService._normalize_history_id(history_id) != history_id:
            return (
                jsonify({"success": False, "error": {"message": "无效的会话 ID", "details": {}}}),
                404,
            )
        service = app.config.get("IMAGE_SERVICE")
        result_dir = getattr(service, "result_dir", None)
        if result_dir is None:
            from .services.image_service import DEFAULT_RESULT_DIR

            result_dir = DEFAULT_RESULT_DIR
        return send_from_directory(result_dir / history_id, filename)

    @app.errorhandler(413)
    def request_too_large(_error):
        return (
            jsonify({"success": False, "error": {"message": "上传内容过大（上限 25MB）", "details": {}}}),
            413,
        )

    _register_blueprints(app)
    return app


def _register_blueprints(app: Flask) -> None:
    """Register route blueprints when their modules are added in the route batch."""
    try:
        from .routes.configs import configs_bp
    except ModuleNotFoundError as exc:
        if exc.name != "backend.routes.configs":
            raise
    else:
        app.register_blueprint(configs_bp, url_prefix="/api/configs")

    try:
        from .routes.generation import generation_bp
    except ModuleNotFoundError as exc:
        if exc.name != "backend.routes.generation":
            raise
    else:
        app.register_blueprint(generation_bp, url_prefix="/api")

    try:
        from .routes.prompt_templates import prompt_templates_bp
    except ModuleNotFoundError as exc:
        if exc.name != "backend.routes.prompt_templates":
            raise
    else:
        app.register_blueprint(prompt_templates_bp, url_prefix="/api/prompt-templates")

    # Beta: decoupled AI-image detection. It must NEVER break app startup, so
    # ANY failure to import or register the optional route is caught and logged
    # with the specific error instead of propagating.
    try:
        from .routes.detection import detection_bp
    except Exception as exc:  # noqa: BLE001 - isolate the optional beta module
        app.logger.warning(
            "[detection] route failed to load; /api/detect disabled: %s: %s",
            type(exc).__name__, exc,
        )
    else:
        app.register_blueprint(detection_bp, url_prefix="/api/detect")
        _log_detection_status(app)


def _log_detection_status(app: Flask) -> None:
    """Probe the optional detection module at startup and log a clear status
    line (ready / degraded / failed). Never raises — a broken beta module must
    not affect the host app."""
    try:
        from detection import detector_health

        report = detector_health()
    except Exception as exc:  # noqa: BLE001
        app.logger.warning(
            "[detection] module failed to load; /api/detect will report unavailable: %s: %s",
            type(exc).__name__, exc,
        )
        return

    if report.get("available"):
        analyzers = report.get("analyzers", {})
        enabled = sum(1 for ok in analyzers.values() if ok)
        missing_opt = report.get("missing_optional") or []
        suffix = f" (optional deps missing: {', '.join(missing_opt)})" if missing_opt else ""
        app.logger.info("[detection] ready: %d/%d analyzers available%s", enabled, len(analyzers), suffix)
    else:
        missing = ", ".join(report.get("missing_required") or []) or "unknown"
        app.logger.warning(
            "[detection] degraded: missing required deps [%s] — run "
            "`pip install -r detection/requirements.txt` and restart to enable",
            missing,
        )


app = create_app()


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug, use_reloader=False)
