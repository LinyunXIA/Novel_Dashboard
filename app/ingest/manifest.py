"""Design_Folder 导入激活清单（`import_files.yaml`）——prod 门控。

数据整理员工作流：把要导入的 Design_Folder 文件在 `import_files.yaml` 置 `active:true`，
各落库入口（ingest / events-movie / events-stock / labor-baseline）在 **prod** 就只导入
激活文件，未激活一律跳过（严格白名单）。**dev/test 不读本清单**，维持全量导入现状。

清单格式：`files: [{path: <相对 Design_Folder 的正斜杠路径>, active: bool}]`。
yaml 读取模式参照 `app/ingest/importers/_client.py` 的 `yaml.safe_load`。
"""
from __future__ import annotations

from pathlib import Path

import typer
import yaml

MANIFEST_NAME = "import_files.yaml"


def load_active_files(source_dir: Path) -> set[str] | None:
    """读 `source_dir/import_files.yaml`，返回 `active:true` 的 Design_Folder 相对路径集。

    - 文件不存在 → 返回 `None`（调用方据此不启用门控，全量导入）；
    - `active:true` 但磁盘上缺失的路径 → 打 warning（不阻断）。
    """
    path = source_dir / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (yaml.YAMLError, OSError) as e:
        typer.secho(f"✗ {MANIFEST_NAME} 解析失败（{path}）：{e}", fg=typer.colors.RED)
        raise typer.Exit(1)

    active: set[str] = set()
    for f in data.get("files") or []:
        if isinstance(f, dict) and f.get("active"):
            rel = str(f.get("path") or "").strip().lstrip("/")
            if not rel:
                continue
            active.add(rel)
            if not (source_dir / rel).exists():
                typer.secho(f"  ⚠ {MANIFEST_NAME} 激活但文件缺失: {rel}",
                            fg=typer.colors.YELLOW)
    return active


def require_active_files(env: str, cfg) -> set[str] | None:
    """门控入口：prod 必读清单（缺失则报错退出）；非 prod 返回 `None`（不启用门控）。

    各落库命令统一调用；返回的激活路径集供上层按文件过滤。
    """
    if env != "prod":
        return None
    active = load_active_files(cfg.source_dir)
    if active is None:
        typer.secho(
            f"✗ {env} 导入依赖 Design_Folder/{MANIFEST_NAME}，但该文件不存在；"
            f"请先创建它并把要导入的文件置 active:true",
            fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"[{env}] 激活清单: {len(active)} 个文件将导入")
    return active