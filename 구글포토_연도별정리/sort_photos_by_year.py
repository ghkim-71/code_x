#!/usr/bin/env python3
"""구글 테이크아웃(Google Takeout)으로 내려받은 구글 포토 사진·동영상을 연도별 폴더로 정리합니다.

촬영 날짜는 다음 순서로 판단합니다.
  1. 테이크아웃이 함께 주는 JSON 메타데이터 파일의 photoTakenTime
  2. 사진 내부 EXIF 촬영일 (Pillow가 설치된 경우)
  3. 파일 이름에 들어 있는 날짜 (예: IMG_20230512_..., 2023-05-12 ...)
  4. 위 방법이 모두 실패하면 "날짜미상" 폴더로 보냅니다.

사용법:
  python3 sort_photos_by_year.py <테이크아웃_압축해제_폴더> <결과_폴더> [--move] [--dry-run]
  python3 sort_photos_by_year.py <원본> <결과> --year 2020 --by-month   # 2020년만 월별로

--by-month 를 주면 "2020년/01월" 처럼 월 폴더까지 만듭니다.
--year 를 주면 그 연도의 사진만 처리합니다.
촬영 시각은 기본적으로 한국 시간(UTC+9)으로 계산합니다(--utc-offset 으로 변경).

기본값은 '복사'이므로 원본은 그대로 남습니다. --move 를 주면 이동합니다.
"""
import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff",
    ".dng", ".raw", ".cr2", ".nef", ".arw",
    ".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".webm",
}
UNKNOWN = "날짜미상"
NAME_DATE = re.compile(r"(19[7-9]\d|20[0-4]\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])")


def find_sidecar(media: Path, json_by_dir: dict) -> Path | None:
    """테이크아웃 JSON 파일 찾기. 이름 규칙이 여러 가지이고 47자 정도에서 잘리기도 합니다."""
    candidates = json_by_dir.get(media.parent, [])
    name = media.name
    stem = media.stem
    m = re.match(r"^(.*?)(\(\d+\))$", stem)  # IMG_1(1).jpg -> IMG_1.jpg(1).json 형태
    dup_variant = f"{m.group(1)}{media.suffix}{m.group(2)}" if m else None
    best = None
    for j in candidates:
        jbase = j.name[:-5]  # ".json" 제거
        if dup_variant and jbase.split(".supplemental")[0] in (dup_variant,):
            return j
        if jbase == name or jbase.startswith(name + "."):
            return j
        # 파일 이름이 잘린 경우: JSON 이름의 앞부분이 미디어 이름의 접두어
        head = jbase.split(".supplemental")[0].split(".suppl")[0]
        if len(head) >= 20 and name.startswith(head.rstrip(".")):
            best = best or j
        elif jbase == stem:
            best = best or j
    return best


def date_from_json(path: Path, tz: timezone) -> tuple | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = int(data["photoTakenTime"]["timestamp"])
        if ts > 0:
            d = datetime.fromtimestamp(ts, tz=tz)
            return d.year, d.month
    except Exception:
        pass
    return None


def date_from_exif(path: Path) -> tuple | None:
    try:
        from PIL import Image  # 선택 사항
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
            if raw:
                y, mo = int(str(raw)[:4]), int(str(raw)[5:7])
                if 1970 <= y <= datetime.now().year and 1 <= mo <= 12:
                    return y, mo
    except Exception:
        pass
    return None


def date_from_name(path: Path) -> tuple | None:
    m = NAME_DATE.search(path.name)
    if m:
        y = int(m.group(1))
        if y <= datetime.now().year:
            return y, int(m.group(2))
    return None


def unique_target(dest_dir: Path, name: str) -> Path:
    target = dest_dir / name
    n = 1
    while target.exists():
        target = dest_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description="구글 포토 테이크아웃 사진을 연도별로 정리")
    ap.add_argument("source", type=Path, help="테이크아웃 압축을 푼 폴더")
    ap.add_argument("dest", type=Path, help="연도별 폴더를 만들 결과 폴더")
    ap.add_argument("--move", action="store_true", help="복사 대신 이동")
    ap.add_argument("--dry-run", action="store_true", help="실제로 옮기지 않고 결과만 표시")
    ap.add_argument("--by-month", action="store_true", help="연도 폴더 안에 월 폴더(01월~12월)까지 생성")
    ap.add_argument("--year", type=int, help="이 연도의 사진만 처리 (예: 2020)")
    ap.add_argument("--utc-offset", type=float, default=9, help="촬영 시각 계산용 시간대 (기본 9 = 한국)")
    args = ap.parse_args()
    tz = timezone(timedelta(hours=args.utc_offset))

    if not args.source.is_dir():
        print(f"원본 폴더가 없습니다: {args.source}", file=sys.stderr)
        return 1

    files = [p for p in args.source.rglob("*") if p.is_file()]
    json_by_dir: dict = {}
    for p in files:
        if p.suffix.lower() == ".json":
            json_by_dir.setdefault(p.parent, []).append(p)
    media = [p for p in files if p.suffix.lower() in MEDIA_EXT]

    counts: dict = {}
    for p in sorted(media):
        sidecar = find_sidecar(p, json_by_dir)
        date = (date_from_json(sidecar, tz) if sidecar else None) or date_from_exif(p) or date_from_name(p)
        if args.year and (not date or date[0] != args.year):
            continue  # 연도를 지정했으면 날짜를 모르는 사진과 다른 연도 사진은 건너뜀
        if not date:
            folder = UNKNOWN
        elif args.by_month:
            folder = f"{date[0]}년/{date[1]:02d}월"
        else:
            folder = f"{date[0]}년"
        counts[folder] = counts.get(folder, 0) + 1
        dest_dir = args.dest / folder
        if args.dry_run:
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = unique_target(dest_dir, p.name)
        (shutil.move if args.move else shutil.copy2)(str(p), str(target))

    print(f"처리한 사진·동영상: {sum(counts.values())}개" + (" (미리보기, 실제 변경 없음)" if args.dry_run else ""))
    for folder in sorted(counts):
        print(f"  {folder}: {counts[folder]}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
