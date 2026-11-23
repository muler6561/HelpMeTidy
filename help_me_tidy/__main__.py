import argparse
import json
import os
import sys
import time
from importlib.metadata import version
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, ValidationError

from .analysis import analyze_scene
from .scene import SceneAnalysis, allocate_id, confirm_scene


def read_scene(path: Path) -> SceneAnalysis:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return SceneAnalysis.model_validate(data.get("scene", data))


def emit_result(data: dict, output: Path | None) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    if output:
        # Exclusive creation prevents accidental loss of a previous analysis.
        with output.open("x", encoding="utf-8") as stream:
            stream.write(payload + "\n")
    print(payload)


class Settings(BaseModel):
    model: str = Field(min_length=1)
    timeout_ms: int = Field(gt=0, le=120000)


def main() -> int:
    parser = argparse.ArgumentParser(description="Help Me Tidy 環境檢查與照片分析")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="離線檢查套件及設定，不呼叫 API")
    analyze = commands.add_parser("analyze", help="傳送一張照片至 Gemini API")
    analyze.add_argument("image", type=Path)
    analyze.add_argument("--prompt", default="請用繁體中文列出照片中可見的物品與區域，描述它們的位置。")
    analyze.add_argument("--output", type=Path, help="保存結果至新 JSON 檔案，不覆寫")
    confirm = commands.add_parser("confirm", help="離線確認修正後的場景，保留原有 ID")
    confirm.add_argument("original", type=Path)
    confirm.add_argument("edited", type=Path)
    confirm.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "confirm":
        try:
            scene = confirm_scene(read_scene(args.original), read_scene(args.edited))
            emit_result({"scene": scene.model_dump(mode="json")}, args.output)
            return 0
        except ValidationError as exc:
            print("場景驗證失敗：" + json.dumps([
                {"path": list(e["loc"]), "type": e["type"]}
                for e in exc.errors()
            ], ensure_ascii=False), file=sys.stderr)
        except (OSError, ValueError, AttributeError) as exc:
            print(f"確認失敗（{type(exc).__name__}）：請檢查 JSON、ID 是否保留及輸出檔是否已存在。", file=sys.stderr)
        return 2
    if args.command == "analyze" and args.output and args.output.exists():
        print("輸出檔已存在，請指定新的檔名。", file=sys.stderr)
        return 2
    load_dotenv()
    try:
        settings = Settings(
            model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            timeout_ms=os.getenv("GEMINI_TIMEOUT_MS", "60000"),
        )
    except ValidationError:
        print("設定錯誤：模型名稱不可空白，逾時需為 1–120000 毫秒。", file=sys.stderr)
        return 2
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if args.command == "check":
        print(json.dumps({
            "python": sys.version.split()[0],
            "packages": {name: version(name) for name in
                         ("google-genai", "pydantic", "Pillow", "python-dotenv")},
            "configured_model": settings.model,
            "api_key_configured": bool(key),
            "api_tested": False,
        }, ensure_ascii=False, indent=2))
        return 0
    if not key:
        print("缺少 GEMINI_API_KEY：請填入本機 .env，不要將金鑰貼到對話或提交 Git。", file=sys.stderr)
        return 2
    try:
        if args.image.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("圖片超過 10 MiB。")
        with Image.open(args.image) as photo:
            if photo.format not in ("JPEG", "PNG"):
                raise ValueError("僅支援 JPEG 或 PNG。")
            photo.verify()
        with Image.open(args.image) as photo:
            photo.load()
            image = photo.convert("RGB")
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        print("圖片錯誤：請提供有效 JPEG／PNG，大小不超過 10 MiB，且像素數不可過大。", file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        with genai.Client(api_key=key, vertexai=False, http_options=types.HttpOptions(
            timeout=settings.timeout_ms,
            retry_options=types.HttpRetryOptions(attempts=1),
        )) as client:
            scene, response = analyze_scene(
                client, image, args.prompt, model=settings.model,
                image_id=allocate_id("image"),
            )
        emit_result({
            "configured_model": settings.model,
            "model_version": response.model_version,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "usage": response.usage_metadata.model_dump(mode="json") if response.usage_metadata else None,
            "scene": scene.model_dump(mode="json"),
        }, args.output)
        return 0
    except (ValidationError, ValueError) as exc:
        detail = ([{"path": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
                  if isinstance(exc, ValidationError) else "結果為空、模型自行確認或提供未量測尺寸；請重新分析。")
        print("結構化結果驗證失敗：" + json.dumps(detail, ensure_ascii=False), file=sys.stderr)
        return 1
    except OSError:
        print("分析或輸出失敗：請檢查連線、輸出目錄權限及檔名是否已存在。", file=sys.stderr)
        return 1
    except errors.APIError as exc:
        reasons = {400: "請求或金鑰設定無效", 401: "金鑰驗證失敗", 403: "金鑰或模型權限不足",
                   404: "模型不存在或無法存取", 429: "配額或頻率限制"}
        print(f"API 失敗（{exc.code}）：{reasons.get(exc.code, '服務端錯誤，請檢查服務狀態')}。", file=sys.stderr)
        return 1
    except Exception as exc:
        # Never echo raw SDK exceptions: they may contain request credentials.
        print(f"呼叫失敗（{type(exc).__name__}）：請檢查網路、逾時或 SDK 設定。", file=sys.stderr)
        return 1
    finally:
        image.close()
        print(f"API elapsed_seconds={time.perf_counter() - started:.3f}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
