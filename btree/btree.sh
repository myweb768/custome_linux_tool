#!/bin/bash

# --- COLOR DEFINITIONS ---
COLOR_RESET="\033[0m"
COLOR_MAIN_DIR="\033[1;33m"  # Bold Yellow
COLOR_SUB_DIR="\033[1;34m"   # Bold Blue
COLOR_TREE="\033[0;36m"      # Cyan
COLOR_STATS="\033[0;90m"     # Dark Gray

# Extension Colors (original set)
COLOR_JS="\033[1;32m"        # Bold Green (.js, .ts, .jsx, .tsx)
COLOR_JSON="\033[0;33m"      # Yellow (.json)
COLOR_ENV="\033[1;31m"       # Bold Red (.env)
COLOR_HTML="\033[0;35m"      # Purple (.html, .htm)
COLOR_CSS="\033[0;36m"       # Cyan (.css, .scss)
COLOR_PY="\033[1;34m"        # Light Blue (.py)
COLOR_GO="\033[1;36m"        # Bright Cyan (.go)
COLOR_CPP="\033[0;33m"       # Brown/Orange (.cpp, .c, .h)
COLOR_PHP="\033[1;35m"       # Light Purple (.php)
COLOR_MD="\033[1;37m"        # Bold White (.md)
COLOR_DEFAULT="\033[0m"      # Default White

# Extension Colors (new)
COLOR_SHELL="\033[38;5;155m"    # Lime green   (.sh, .bash, .zsh, .fish)
COLOR_YAML="\033[38;5;222m"     # Light gold   (.yml, .yaml, .toml, .ini, .conf, .cfg)
COLOR_XML="\033[38;5;135m"      # Violet       (.xml)
COLOR_SQL="\033[38;5;75m"       # Sky blue     (.sql)
COLOR_RUBY="\033[38;5;197m"     # Ruby pink    (.rb)
COLOR_RUST="\033[38;5;208m"     # Rust orange  (.rs)
COLOR_JAVA="\033[38;5;173m"     # Tan          (.java, .class, .jar)
COLOR_KOTLIN="\033[38;5;141m"   # Purple       (.kt, .kts)
COLOR_SWIFT="\033[38;5;202m"    # Orange-red   (.swift)
COLOR_DART="\033[38;5;39m"      # Blue         (.dart)
COLOR_VUE="\033[38;5;42m"       # Emerald      (.vue, .svelte)
COLOR_LOCK="\033[38;5;244m"     # Gray         (.lock, .gitignore, LICENSE, ...)
COLOR_DOCKER="\033[38;5;33m"    # Docker blue  (Dockerfile)
COLOR_ARCHIVE="\033[38;5;214m"  # Orange       (.zip, .tar, .gz, ...)
COLOR_IMAGE="\033[38;5;219m"    # Pink         (.png, .jpg, .svg, ...)
COLOR_DOC="\033[38;5;253m"      # Light gray   (.pdf, .docx, .txt, ...)
COLOR_LOG="\033[38;5;240m"      # Dim gray     (.log)

# Default Values
IGNORE_PATTERN=""
MAX_DEPTH=9999
SHOW_ALL=false
NO_COLOR_FLAG=false
DIRS_ONLY=false
DETAILS=false
OUTPUT_FILE=""
TOTAL_DIRS=0
TOTAL_FILES=0

# Function to show help menu
show_help() {
    echo -e "${COLOR_MAIN_DIR}btree (Butter Tree) - Advanced Directory Tree Viewer${COLOR_RESET}"
    echo ""
    echo -e "Usage: btree [OPTIONS] [DIRECTORY]"
    echo ""
    echo "Options:"
    echo "  -a, --all         Show all files (including hidden files like .git, .env)"
    echo "  -d                Show directories only (no files)"
    echo "  -ds               Directories only, with recursive file/size/extension stats"
    echo "  -I <pattern>      Ignore files or directories matching the pattern (e.g. node_modules)"
    echo "  -L <level>        Descend only 'level' directories deep"
    echo "  --no-color        Disable colored output"
    echo "  -o <file>         Save output to a file instead of the terminal"
    echo "  -h, --help        Show this help menu"
    echo ""
    echo "Examples:"
    echo "  btree -I node_modules"
    echo "  btree -a -I \"node_modules|.git\""
    echo "  btree -L 2"
    echo "  btree -d"
    echo "  btree -ds"
    echo "  btree -o myReport.txt"
    echo "  btree --no-color > tree.txt"
    exit 0
}

# Parse Arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -I)
            IGNORE_PATTERN="$2"
            shift 2
            ;;
        -L)
            MAX_DEPTH="$2"
            shift 2
            ;;
        -a|--all)
            SHOW_ALL=true
            shift
            ;;
        -d)
            DIRS_ONLY=true
            shift
            ;;
        -ds)
            DIRS_ONLY=true
            DETAILS=true
            shift
            ;;
        --no-color)
            NO_COLOR_FLAG=true
            shift
            ;;
        -o)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            ;;
        *)
            TARGET_DIR="$1"
            shift
            ;;
    esac
done

TARGET_DIR="${TARGET_DIR:-.}"

# If -o was given, send all tree output to that file instead of the
# terminal. This happens *before* the terminal check below, so writing
# to a file automatically behaves like piping - colors turn off on
# their own, same as `btree > file.txt` would.
if [ -n "$OUTPUT_FILE" ]; then
    exec > "$OUTPUT_FILE"
fi

# Disable colors automatically when: explicitly requested, the NO_COLOR
# convention is set (https://no-color.org), or output isn't a terminal
# (e.g. piped to a file or `less`) - matches how most modern CLI tools behave.
if [ "$NO_COLOR_FLAG" = true ] || [ -n "$NO_COLOR" ] || [ ! -t 1 ]; then
    for var in $(compgen -v COLOR_); do
        eval "$var=''"
    done
fi

# Function to get file color based on extension / filename
get_file_color() {
    local filename="$1"
    case "$filename" in
        Dockerfile|dockerfile|Dockerfile.*)     echo -e "$COLOR_DOCKER" ;;
        Makefile|makefile)                      echo -e "$COLOR_TREE" ;;
        README*|readme*)                        echo -e "$COLOR_MD" ;;
        LICENSE*|license*|.gitignore|.gitattributes|.dockerignore|*.lock)
                                                 echo -e "$COLOR_LOCK" ;;
        *.js|*.jsx|*.ts|*.tsx)                  echo -e "$COLOR_JS" ;;
        *.json)                                 echo -e "$COLOR_JSON" ;;
        .env*)                                  echo -e "$COLOR_ENV" ;;
        *.html|*.htm)                           echo -e "$COLOR_HTML" ;;
        *.css|*.scss|*.sass)                    echo -e "$COLOR_CSS" ;;
        *.py)                                   echo -e "$COLOR_PY" ;;
        *.go)                                   echo -e "$COLOR_GO" ;;
        *.cpp|*.c|*.h|*.hpp)                    echo -e "$COLOR_CPP" ;;
        *.php)                                  echo -e "$COLOR_PHP" ;;
        *.md)                                   echo -e "$COLOR_MD" ;;
        *.sh|*.bash|*.zsh|*.fish)               echo -e "$COLOR_SHELL" ;;
        *.yml|*.yaml|*.toml|*.ini|*.conf|*.cfg) echo -e "$COLOR_YAML" ;;
        *.xml)                                  echo -e "$COLOR_XML" ;;
        *.sql)                                  echo -e "$COLOR_SQL" ;;
        *.rb)                                   echo -e "$COLOR_RUBY" ;;
        *.rs)                                   echo -e "$COLOR_RUST" ;;
        *.java|*.class|*.jar)                   echo -e "$COLOR_JAVA" ;;
        *.kt|*.kts)                             echo -e "$COLOR_KOTLIN" ;;
        *.swift)                                echo -e "$COLOR_SWIFT" ;;
        *.dart)                                 echo -e "$COLOR_DART" ;;
        *.vue|*.svelte)                         echo -e "$COLOR_VUE" ;;
        *.zip|*.tar|*.gz|*.tgz|*.rar|*.7z|*.bz2) echo -e "$COLOR_ARCHIVE" ;;
        *.png|*.jpg|*.jpeg|*.gif|*.svg|*.webp|*.ico|*.bmp) echo -e "$COLOR_IMAGE" ;;
        *.pdf|*.doc|*.docx|*.txt|*.odt)         echo -e "$COLOR_DOC" ;;
        *.log)                                  echo -e "$COLOR_LOG" ;;
        *)                                       echo -e "$COLOR_DEFAULT" ;;
    esac
}

# Files where counting words/characters is meaningless (binary formats) -
# skip the wc calls entirely for these instead of running them on garbage.
is_binary_like() {
    case "$1" in
        *.zip|*.tar|*.gz|*.tgz|*.rar|*.7z|*.bz2|*.jar|*.class) return 0 ;;
        *.png|*.jpg|*.jpeg|*.gif|*.svg|*.webp|*.ico|*.bmp)     return 0 ;;
        *.pdf|*.doc|*.docx|*.odt)                              return 0 ;;
        *)                                                     return 1 ;;
    esac
}

# For -ds: recursively totals up every file *under* a directory (all
# nested subfolders included) into a file count, a total size, and a
# per-extension breakdown, e.g. "12 files, 340KB (js:6, css:3, json:3)".
compute_dir_stats() {
    local dir="$1"
    local file_list

    if [ -n "$IGNORE_PATTERN" ]; then
        file_list=$(find "$dir" -type f 2>/dev/null | grep -Ev "$IGNORE_PATTERN")
    else
        file_list=$(find "$dir" -type f 2>/dev/null)
    fi

    local total_files=0
    local total_bytes=0
    declare -A ext_counts=()

    while IFS= read -r f; do
        [ -z "$f" ] && continue
        total_files=$((total_files + 1))

        local sz
        sz=$(stat -c%s "$f" 2>/dev/null)
        total_bytes=$((total_bytes + ${sz:-0}))

        local base="${f##*/}"
        local ext="${base##*.}"
        if [ "$ext" = "$base" ]; then
            ext="noext"
        fi
        ext_counts["$ext"]=$(( ${ext_counts["$ext"]:-0} + 1 ))
    done <<< "$file_list"

    local total_kb=$(( total_bytes / 1024 ))

    # Sort extensions by count, descending, so the most common file type
    # in that folder shows up first.
    local ext_summary
    ext_summary=$(
        for ext in "${!ext_counts[@]}"; do
            echo "${ext_counts[$ext]} $ext"
        done | sort -rn | awk '{print $2":"$1}' | awk 'BEGIN{ORS=""} {print (NR>1?", ":"") $0}'
    )

    if [ "$total_files" -eq 0 ]; then
        echo "empty"
    else
        echo "${total_files} files, ${total_kb}KB${ext_summary:+, $ext_summary}"
    fi
}

# Function to print directory recursively
walk_dir() {
    local dir="$1"
    local prefix="$2"
    local depth="$3"

    if [ "$depth" -gt "$MAX_DEPTH" ]; then
        return
    fi

    local entries=()

    if [ "$SHOW_ALL" = true ]; then
        while IFS= read -r line; do
            entries+=("$line")
        done < <(ls -A1 "$dir" 2>/dev/null | sort)
    else
        while IFS= read -r line; do
            entries+=("$line")
        done < <(ls -1 "$dir" 2>/dev/null | sort)
    fi

    # Apply the ignore pattern and (if -d) drop files up front, before
    # figuring out which item is "last" - otherwise an ignored/filtered
    # trailing item makes the connector land on the wrong line (└── vs ├──).
    local visible=()
    for item in "${entries[@]}"; do
        if [ -n "$IGNORE_PATTERN" ] && [[ "$item" =~ $IGNORE_PATTERN ]]; then
            continue
        fi
        if [ "$DIRS_ONLY" = true ] && [ ! -d "$dir/$item" ]; then
            continue
        fi
        visible+=("$item")
    done

    local count=${#visible[@]}
    local i=0

    for item in "${visible[@]}"; do
        i=$((i + 1))

        local full_path="$dir/$item"
        local pointer="${COLOR_TREE}├── ${COLOR_RESET}"
        local ext_prefix="${COLOR_TREE}│   ${COLOR_RESET}"

        if [ "$i" -eq "$count" ]; then
            pointer="${COLOR_TREE}└── ${COLOR_RESET}"
            ext_prefix="    "
        fi

        if [ -d "$full_path" ]; then
            TOTAL_DIRS=$((TOTAL_DIRS + 1))
            if [ "$DETAILS" = true ]; then
                local dstats
                dstats=$(compute_dir_stats "$full_path")
                echo -e "${prefix}${pointer}${COLOR_SUB_DIR}${item}/${COLOR_RESET} ${COLOR_STATS}(${dstats})${COLOR_RESET}"
            else
                echo -e "${prefix}${pointer}${COLOR_SUB_DIR}${item}/${COLOR_RESET}"
            fi
            walk_dir "$full_path" "${prefix}${ext_prefix}" "$((depth + 1))"
        elif [ -f "$full_path" ]; then
            TOTAL_FILES=$((TOTAL_FILES + 1))
            local size=$(numfmt --to=iec-i --suffix=B $(stat -c%s "$full_path") 2>/dev/null || du -h "$full_path" 2>/dev/null | cut -f1)

            local chars words
            if is_binary_like "$item"; then
                chars="binary"
                words="binary"
            else
                # One wc call instead of two (-w -m together) - GNU wc
                # always prints columns in the fixed order: words, chars.
                read -r words chars <<< "$(wc -wm < "$full_path" 2>/dev/null)"
                chars="${chars:-0}"
                words="${words:-0}"
            fi

            local file_color=$(get_file_color "$item")

            echo -e "${prefix}${pointer}${file_color}${item}${COLOR_RESET} ${COLOR_STATS}(${size:-0B}, ${chars}ch, ${words}w)${COLOR_RESET}"
        fi
    done
}

# Main Root Directory Name
echo -e "${COLOR_MAIN_DIR}${TARGET_DIR}${COLOR_RESET}"
walk_dir "$TARGET_DIR" "" 1

echo ""
if [ "$DIRS_ONLY" = true ]; then
    echo -e "${COLOR_STATS}${TOTAL_DIRS} directories${COLOR_RESET}"
else
    echo -e "${COLOR_STATS}${TOTAL_DIRS} directories, ${TOTAL_FILES} files${COLOR_RESET}"
fi

if [ -n "$OUTPUT_FILE" ]; then
    echo "✓ Tree saved to $OUTPUT_FILE" >&2
fi
