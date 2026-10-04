"""Build a Python 3.12 bytecode runtime archive without source or credentials."""
import hashlib
import json
import py_compile
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

if sys.version_info[:2] != (3, 12):
    raise SystemExit("Run with: uv run --no-project --python 3.12 scripts/package.py")

root = Path(__file__).resolve().parents[1]
tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
release = root / "artifacts" / tag
release.mkdir(parents=True, exist_ok=False)
sources = [root / "main.py"]
for directory in ("config", "core", "parser", "util"):
    sources.extend(sorted((root / directory).glob("*.py")))
for source in sources:
    relative = source.relative_to(root)
    target = release / "app" / relative.with_suffix(".pyc")
    target.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(source), cfile=str(target),
                       dfile="/app/" + relative.as_posix(), doraise=True)

subprocess.run([
    "uv", "export", "--frozen", "--no-dev", "--no-emit-project",
    "--format", "requirements-txt", "--output-file", str(release / "requirements.txt"),
], cwd=root, check=True, stdout=subprocess.DEVNULL)
for name in ("Dockerfile", "compose.yaml", ".env.example"):
    shutil.copyfile(root / "deploy" / name, release / name)
manifest = {"image_tag": tag, "python": "3.12", "files": {
    p.relative_to(release).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted(release.rglob("*")) if p.is_file()
}}
(release / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
archive = root / "artifacts" / f"anydownloadscript-{tag}.tar.gz"
with tarfile.open(archive, "w:gz") as tar:
    for path in sorted(release.rglob("*")):
        if path.is_file():
            tar.add(path, arcname=path.relative_to(release).as_posix())
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix(archive.suffix + ".sha256").write_text(
    f"{digest}  {archive.name}\n", encoding="utf-8")
print(json.dumps({"tag": tag, "archive": str(archive), "sha256": digest}))
