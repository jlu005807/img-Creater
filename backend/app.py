import os

from flask import Flask, jsonify
from flask_cors import CORS


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    @app.get("/api/health")
    def health_check():
        return jsonify({"ok": True, "service": "gpt-img2-creater-backend"})

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


app = create_app()


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug, use_reloader=False)
