#!/usr/bin/env bash
# modern-cli.sh — interactive aliases for modern CLI replacements.
# Deployed by ansible/bootstrap.yml to ~/.bashrc.d/50-modern-cli.sh
# (Fedora Atomic auto-sources ~/.bashrc.d/*; bootstrap also ensures the sourcing line exists).
#
# The ORIGINAL tools are always still available:
#   - real binary:            /usr/bin/ls   /usr/bin/cat   /usr/bin/vim
#   - bypass the alias once:   \ls          command cat
#   - scripts are unaffected:  aliases don't expand in non-interactive shells
#
# Each block is guarded by `command -v`, so this same file is safe on the host AND inside the
# meld-dev distrobox (a shared $HOME) — an alias is only set where its tool is actually installed.

# Ensure ~/.local/bin is on PATH (Claude Code + Codex native installers land here; shared into the box).
case ":${PATH}:" in
  *":${HOME}/.local/bin:"*) ;;
  *) export PATH="${HOME}/.local/bin:${PATH}" ;;
esac

# ls -> eza   (icons need a Nerd Font; drop `--icons=auto` if you haven't set one, or you get tofu)
if command -v eza >/dev/null 2>&1; then
  alias ls='eza --group-directories-first --icons=auto'
  alias ll='eza -l  --group-directories-first --icons=auto --git'
  alias la='eza -la --group-directories-first --icons=auto --git'
  alias lt='eza --tree --level=2 --group-directories-first --icons=auto'
fi

# cat -> bat   (--paging=never keeps cat's dump-don't-page behavior; bat auto-detects pipes and
#               falls back to plain passthrough, so `cat file | grep ...` is unchanged.
#               For zero decoration use `bat -p`; set a default style in ~/.config/bat/config.)
if command -v bat >/dev/null 2>&1; then
  alias cat='bat --paging=never'
elif command -v batcat >/dev/null 2>&1; then    # Debian/Ubuntu rename (e.g. an apt-based distrobox)
  alias cat='batcat --paging=never'
  alias bat='batcat'
fi

# vim -> nvim   (and make nvim the editor programs spawn: git commit, sudoedit, crontab, ...)
if command -v nvim >/dev/null 2>&1; then
  alias vim='nvim'
  # alias vi='nvim'          # uncomment if you also want bare `vi` to open nvim
  export EDITOR='nvim'
  export VISUAL='nvim'
fi
