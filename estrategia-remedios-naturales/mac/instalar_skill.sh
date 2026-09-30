#!/bin/bash
# Instala la skill /remedios para Claude Code (en ~/.claude/skills) apuntando a este proyecto.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$HERE")"
DEST="$HOME/.claude/skills/remedios"
mkdir -p "$DEST"
sed "s|{{PROJECT_DIR}}|$PROJECT|g" "$PROJECT/skill/remedios/SKILL.md" > "$DEST/SKILL.md"
echo "Skill instalada: $DEST/SKILL.md"
echo "Abre Claude Code y escribe:  /remedios <video_en_ingles> <imagen_avatar>"
