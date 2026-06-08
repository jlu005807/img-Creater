import os

from flask import Flask, jsonify
from flask_cors import CORS


def create_app() -> Flask:
    app = Flask(__name__)
    # Local single-user tool: only the Vite dev origin needs cross-origin access.
    allowed = os.getenv("FRONTEND_ORIGIN", "http://127.0.0.1:5173,http://localhost:5173").split(",")
    CORS(app, resources={r"/api/*": {"origins": [o.strip() for o in allowed if o.strip()]}})

    # Cap request bodies so oversized base64 image/mask uploads can't exhaust
    # memory; edits carry the original image + mask inline.
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

    # One app-scoped ImageService so its requests.Session connection pool is
    # reused across tasks (the detached worker threads outlive each request).
    if "IMAGE_SERVICE" not in app.config:
        from .services.config_service import ConfigService
        from .services.image_service import ImageService

        app.config["IMAGE_SERVICE"] = ImageService(config_service=ConfigService())

    @app.get("/api/health")
    def health_check():
        return jsonify({"ok": True, "service": "gpt-img2-creater-backend"})

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

    # Beta: decoupled AI-image detection. Registers only if the route module
    # is present; the route itself further degrades if detection deps are absent.
    try:
        from .routes.detection import detection_bp
    except ModuleNotFoundError as exc:
        if exc.name != "backend.routes.detection":
            raise
    else:
        app.register_blueprint(detection_bp, url_prefix="/api/detect")


app = create_app()


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug, use_reloader=False)
