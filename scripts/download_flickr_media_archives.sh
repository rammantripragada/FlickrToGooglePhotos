#!/bin/zsh
# Download Flickr Data media ZIPs safely and resumably.
# Usage: ./scripts/download_flickr_media_archives.sh 'https://downloads.flickr.com/d/..._1.zip'

set -euo pipefail

first_url="${1:-}"
destination="/Volumes/T7/FlickrData"
archive_count=486

if [[ -z "$first_url" || "$first_url" != *_1.zip ]]; then
  print -u2 "Usage: $0 'https://downloads.flickr.com/d/..._1.zip'"
  exit 2
fi
if [[ ! -d /Volumes/T7 ]]; then
  print -u2 "T7 is not mounted at /Volumes/T7. Connect and unlock it, then retry."
  exit 2
fi

mkdir -p "$destination"
prefix="${first_url%_1.zip}"

for number in {1..486}; do
  filename="${prefix##*/}_${number}.zip"
  final_path="$destination/$filename"
  partial_path="$final_path.part"
  url="${prefix}_${number}.zip"
  if [[ -s "$final_path" ]]; then
    print "[$number/$archive_count] Already downloaded: $filename"
    continue
  fi
  print "[$number/$archive_count] Downloading: $filename"
  curl --fail --location --retry 8 --retry-all-errors --retry-delay 5 \
    --connect-timeout 30 --continue-at - --output "$partial_path" "$url"
  mv "$partial_path" "$final_path"
  sleep 1
done
print "Completed. Media ZIPs are in: $destination"
