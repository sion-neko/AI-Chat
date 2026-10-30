"""配布用のzipを作る。

  python scripts/make_dist.py --env dist.env

--env に渡したファイルを .env として同梱する（配布先で使うLLMの設定を書いておく）。
省略すると .env は入れず、受け取った人が .env.example から作る。
個人のトークン（GitHub・GitLab・ZulipのトークンやAPIキー）が書かれた .env は同梱しない。

できるもの: dist/ai-chat-YYYYMMDD.zip（中身は ai-chat/ フォルダ）
"""

import argparse
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 動かすのに必要なものだけ入れる（テストや開発用のメモは入れない）
INCLUDE = ["app", "mock_llm", "Dockerfile", "compose.yaml", "requirements.txt", ".env.example", "README.md", "LICENSE"]
SKIP = {"__pycache__"}
# LLM_API_KEY は配布先で共通に使うものなので対象外
PERSONAL_KEYS = re.compile(r"^\s*(GITHUB_TOKEN|GITLAB_TOKEN|ZULIP_API_KEY|ZULIP_EMAIL)\s*=\s*\S", re.MULTILINE)
FOLDER = "ai-chat"


def files() -> list[Path]:
    result = []
    for name in INCLUDE:
        path = ROOT / name
        if path.is_dir():
            result += [p for p in sorted(path.rglob("*")) if p.is_file() and not SKIP & set(p.parts) and p.suffix != ".pyc"]
        else:
            result.append(path)
    return result


def main():
    parser = argparse.ArgumentParser(description="配布用のzipを作る")
    parser.add_argument("--env", type=Path, help="同梱する .env（配布先のLLMの設定）")
    args = parser.parse_args()

    env_text = None
    if args.env:
        env_text = args.env.read_text(encoding="utf-8")
        if m := PERSONAL_KEYS.search(env_text):
            sys.exit(f"{args.env} に個人の設定（{m[1]}）が入っています。配布用の .env からは消してください")

    out = ROOT / "dist" / f"{FOLDER}-{date.today():%Y%m%d}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files():
            z.write(path, f"{FOLDER}/{path.relative_to(ROOT).as_posix()}")
        if env_text is not None:
            z.writestr(f"{FOLDER}/.env", env_text)
    print(out)


if __name__ == "__main__":
    main()
