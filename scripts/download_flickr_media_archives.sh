#!/bin/zsh
# Download Flickr Data media ZIPs safely and resumably.
# Usage: ./scripts/download_flickr_media_archives.sh 'https://downloads.flickr.com/d/..._1.zip' [parallel_jobs]

set -euo pipefail

first_url="${1:-}"
destination="${FLICKR_ARCHIVE_DESTINATION:-/Volumes/T7/FlickrData}"
# Colon-separated folders containing ZIPs already completed on other disks.
existing_dirs="${FLICKR_ARCHIVE_EXISTING_DIRS:-}"
archive_count=486
parallel_jobs="${2:-3}"

if [[ -z "$first_url" || "$first_url" != *_1.zip ]]; then
  print -u2 "Usage: $0 'https://downloads.flickr.com/d/..._1.zip' [parallel_jobs]"
  exit 2
fi
if [[ ! "$parallel_jobs" =~ '^[1-4]$' ]]; then
  print -u2 "parallel_jobs must be a number from 1 to 4 (default: 3)."
  exit 2
fi
if [[ ! -d "${destination:h}" ]]; then
  print -u2 "Destination volume is not mounted: ${destination:h}"
  exit 2
fi

mkdir -p "$destination"
prefix="${first_url%_1.zip}"

download_one() {
  local number="$1"
  filename="${prefix##*/}_${number}.zip"
  final_path="$destination/$filename"
  partial_path="$final_path.part"
  url="${prefix}_${number}.zip"
  if [[ -s "$final_path" ]]; then
    print "[$number/$archive_count] Already downloaded: $filename"
    continue
  fi
  for existing_dir in ${(s/:/)existing_dirs}; do
    if [[ -s "$existing_dir/$filename" ]]; then
      print "[$number/$archive_count] Already downloaded on another disk: $filename"
      return
    fi
  done
  print "[$number/$archive_count] Downloading: $filename"
  curl --fail --location --retry 8 --retry-all-errors --retry-delay 5 \
    --connect-timeout 30 --continue-at - --output "$partial_path" "$url"
  mv "$partial_path" "$final_path"
  sleep 1
}

typeset -a active_pids
for number in {1..486}; do
  download_one "$number" &
  active_pids+=("$!")
  if (( ${#active_pids} >= parallel_jobs )); then
    wait "${active_pids[1]}"
    active_pids=("${active_pids[@]:1}")
  fi
done
for pid in "${active_pids[@]}"; do
  wait "$pid"
done
print "Completed. Media ZIPs are in: $destination"
