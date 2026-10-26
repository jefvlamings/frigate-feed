#!/usr/bin/env bash
# Fetch freely licensed sample clips for the dev Frigate fake cameras.
#
# Output (gitignored, mounted read-only into frigate-dev at /media/dev):
#   dev/media/front_door.mp4  people, bicycles and cars
#   dev/media/driveway.mp4    cars followed by people walking a dog
#
# Sources and licenses:
#   - person-bicycle-car-detection.mp4, car-detection.mp4
#       Intel IoT DevKit sample videos, https://github.com/intel-iot-devkit/sample-videos
#       License: CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)
#   - "Dog walking in the Mission in San Francisco.ogv"
#       Sage Ross (User:Ragesoss), Wikimedia Commons,
#       https://commons.wikimedia.org/wiki/File:Dog_walking_in_the_Mission_in_San_Francisco.ogv
#       License: CC BY-SA 3.0 (https://creativecommons.org/licenses/by-sa/3.0/)
#
# The clips are normalized to H.264 1280x720 @ 15 fps with a 1 s GOP (no audio) so go2rtc
# can loop them with `-c copy`. Transcoding runs in a throwaway container from the Frigate
# image (it ships ffmpeg), so no host ffmpeg is needed.
#
# If a download fails, a synthetic test pattern is generated instead. Frigate will stream it
# but the CPU detector will not find objects in it.
#
# Usage: ./scripts/fetch-dev-media.sh [--force]

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEDIA_DIR="${ROOT_DIR}/dev/media"
RAW_DIR="${MEDIA_DIR}/raw"
FRIGATE_IMAGE="ghcr.io/blakeblackshear/frigate:0.18.2"
FFMPEG_BIN="/usr/lib/ffmpeg/7.0/bin/ffmpeg"

INTEL_BASE="https://github.com/intel-iot-devkit/sample-videos/raw/master"
URL_PERSON_BIKE_CAR="${INTEL_BASE}/person-bicycle-car-detection.mp4"
URL_CAR="${INTEL_BASE}/car-detection.mp4"
URL_DOG="https://upload.wikimedia.org/wikipedia/commons/6/67/Dog_walking_in_the_Mission_in_San_Francisco.ogv"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

mkdir -p "${MEDIA_DIR}" "${RAW_DIR}"

# Run ffmpeg from the Frigate image with dev/media mounted at /work.
ffmpeg_in_frigate() {
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    --network none \
    -v "${MEDIA_DIR}:/work" \
    --entrypoint "${FFMPEG_BIN}" \
    "${FRIGATE_IMAGE}" \
    -hide_banner -loglevel error -y "$@"
}

ENCODE_ARGS=(-an -c:v libx264 -preset veryfast -crf 26 -pix_fmt yuv420p -g 15 -keyint_min 15 -sc_threshold 0 -movflags +faststart)
NORMALIZE="scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=15"

download() {
  local url="$1" dest="$2"
  if [[ -s "${dest}" && "${FORCE}" -eq 0 ]]; then
    return 0
  fi
  echo "Downloading ${url}"
  if curl -fsSL --retry 3 --connect-timeout 20 -A "frigate-feed-dev-media/1.0" -o "${dest}.part" "${url}"; then
    mv "${dest}.part" "${dest}"
    return 0
  fi
  rm -f "${dest}.part"
  echo "WARN: download failed: ${url}" >&2
  return 1
}

test_pattern() {
  local out="$1" text="$2"
  echo "Generating test pattern ${out} (no detectable objects)"
  ffmpeg_in_frigate -f lavfi -i "testsrc2=size=1280x720:rate=15:duration=60" \
    -vf "drawtext=text='${text}':fontsize=48:fontcolor=white:x=40:y=40" \
    "${ENCODE_ARGS[@]}" "/work/${out}" \
    || ffmpeg_in_frigate -f lavfi -i "testsrc2=size=1280x720:rate=15:duration=60" \
      "${ENCODE_ARGS[@]}" "/work/${out}"
}

build_front_door() {
  local out="front_door.mp4"
  if [[ -s "${MEDIA_DIR}/${out}" && "${FORCE}" -eq 0 ]]; then
    echo "Keeping existing dev/media/${out}"
    return
  fi
  if download "${URL_PERSON_BIKE_CAR}" "${RAW_DIR}/person-bicycle-car-detection.mp4"; then
    echo "Encoding dev/media/${out}"
    ffmpeg_in_frigate -i /work/raw/person-bicycle-car-detection.mp4 \
      -vf "${NORMALIZE}" "${ENCODE_ARGS[@]}" "/work/${out}"
  else
    test_pattern "${out}" "front_door"
  fi
}

build_driveway() {
  local out="driveway.mp4"
  if [[ -s "${MEDIA_DIR}/${out}" && "${FORCE}" -eq 0 ]]; then
    echo "Keeping existing dev/media/${out}"
    return
  fi
  local have_car=0 have_dog=0
  download "${URL_CAR}" "${RAW_DIR}/car-detection.mp4" && have_car=1
  download "${URL_DOG}" "${RAW_DIR}/dog-walking.ogv" && have_dog=1

  echo "Encoding dev/media/${out}"
  if [[ "${have_car}" -eq 1 && "${have_dog}" -eq 1 ]]; then
    ffmpeg_in_frigate -i /work/raw/car-detection.mp4 -i /work/raw/dog-walking.ogv \
      -filter_complex "[0:v]${NORMALIZE}[a];[1:v]${NORMALIZE}[b];[a][b]concat=n=2:v=1:a=0[v]" \
      -map "[v]" "${ENCODE_ARGS[@]}" "/work/${out}"
  elif [[ "${have_dog}" -eq 1 ]]; then
    ffmpeg_in_frigate -i /work/raw/dog-walking.ogv -vf "${NORMALIZE}" "${ENCODE_ARGS[@]}" "/work/${out}"
  elif [[ "${have_car}" -eq 1 ]]; then
    ffmpeg_in_frigate -i /work/raw/car-detection.mp4 -vf "${NORMALIZE}" "${ENCODE_ARGS[@]}" "/work/${out}"
  else
    test_pattern "${out}" "driveway"
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required (ffmpeg runs from the Frigate image)." >&2
  exit 1
fi

build_front_door
build_driveway

echo "Done:"
ls -lh "${MEDIA_DIR}"/*.mp4
