from pathlib import Path
import getpass
import grp
import os


TEMPLATES = (
    ("com.discordbot.hsr.plist.example", "com.discordbot.hsr.plist"),
    ("com.discordbot.hsr-backup.plist.example", "com.discordbot.hsr-backup.plist"),
    ("com.discordbot.hsr.newsyslog.conf.example", "com.discordbot.hsr.conf"),
)


def render_templates(project_root: Path, output_dir: Path, user: str, group: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    replacements = {
        "__PROJECT_ROOT__": str(project_root),
        "__USER__": user,
        "__GROUP__": group,
    }
    for template_name, output_name in TEMPLATES:
        rendered = (Path(__file__).parent / template_name).read_text(encoding="utf-8")
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        if any(placeholder in rendered for placeholder in replacements):
            raise RuntimeError(f"unrendered placeholder in {template_name}")
        (output_dir / output_name).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    render_templates(
        root,
        root / "runtime/generated/macos",
        getpass.getuser(),
        grp.getgrgid(os.getgid()).gr_name,
    )
