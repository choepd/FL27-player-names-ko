# -*- coding: utf-8 -*-
"""CSV 한글 명단으로 data_s2627.cpk(Player.bin) 한글화본을 만듭니다.

단일 파일 구성: CRI @UTF 처리 + WESYS zlib + CPK 재구성.

필수:
  --csv         선수명단 CSV (열: id, korean_name, ...)
  --input-cpk   영문(미한글) 기준 data_s2627.cpk
  --output-cpk  생성할 한글 CPK 경로

선택:
  --dry-run     검증·통계만 하고 파일을 쓰지 않습니다.
  --apply PATH  생성 후 게임 CPK 경로에 백업하고 교체합니다.

예:
  python csv_to_hangul_cpk.py --csv 선수명단_v10.csv --input-cpk 원본/data_s2627.cpk --output-cpk data_s2627_hangul.cpk
  python csv_to_hangul_cpk.py --csv 선수명단_v10.csv --input-cpk 원본/data_s2627.cpk --output-cpk out.cpk --dry-run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import re
import shutil
import struct
import sys
import time
import zlib
from pathlib import Path

RS = 312
ID_OFF = 8
NAME_LEN = 61
NAME_OFF_68 = 68
NAME_OFF_251 = 251
HANGUL_ONLY = re.compile(r"^[\uac00-\ud7a3 \-]+$")

log = logging.getLogger("csv_to_hangul_cpk")

# ---------------------------------------------------------------------------
# CRI @UTF (cpk_utf 통합)
# ---------------------------------------------------------------------------
TYPE_FMT = {0: ">B", 1: ">b", 2: ">H", 3: ">h", 4: ">I", 5: ">i", 6: ">Q", 7: ">q", 8: ">f"}


class Col:
    def __init__(self, name, flag, const):
        self.name, self.flag, self.const = name, flag, const
        self.type, self.storage = flag & 0x0F, flag & 0xF0
        self.row_pos = None


class UtfTable:
    def __init__(self, buf, off):
        if buf[off:off + 4] != b"@UTF":
            raise ValueError(f"@UTF 없음 off={off}")
        self.buf, self.off = buf, off
        self.size, = struct.unpack_from(">I", buf, off + 4)
        b = off + 8
        self.row_offset, = struct.unpack_from(">H", buf, b + 2)
        self.str_off, self.data_off, self.name_off = struct.unpack_from(">III", buf, b + 4)
        self.ncols, self.row_width, self.rows = struct.unpack_from(">HHI", buf, b + 16)
        p = off + 32
        self.cols = []
        rp = 0
        for _ in range(self.ncols):
            flag = buf[p]
            name = self._str(struct.unpack_from(">I", buf, p + 1)[0])
            p += 5
            const = None
            if flag & 0xF0 == 0x30:
                const, n = self._val(flag & 0x0F, p)
                p += n
            c = Col(name, flag, const)
            if c.storage == 0x50:
                c.row_pos = rp
                rp += self._size(c.type)
            self.cols.append(c)
        if rp != self.row_width:
            raise ValueError(f"행 폭 불일치 계산={rp} 헤더={self.row_width}")

    def _str(self, rel):
        a = self.off + 8 + self.str_off + rel
        return self.buf[a:self.buf.index(b"\x00", a)].decode("utf-8", "replace")

    @staticmethod
    def _size(t):
        return {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 8, 7: 8, 8: 4, 0xA: 4, 0xB: 8}[t]

    def _val(self, t, p):
        if t in TYPE_FMT:
            return struct.unpack_from(TYPE_FMT[t], self.buf, p)[0], self._size(t)
        if t == 0xA:
            return self._str(struct.unpack_from(">I", self.buf, p)[0]), 4
        if t == 0xB:
            return struct.unpack_from(">II", self.buf, p), 8
        raise ValueError(f"알 수 없는 타입 {t}")

    def cell_pos(self, i, name):
        c = next(c for c in self.cols if c.name == name)
        if c.storage != 0x50:
            raise ValueError(f"행 저장 컬럼 아님 {name} storage={c.storage:#x}")
        return self.off + 8 + self.row_offset + i * self.row_width + c.row_pos, c.type

    def row(self, i):
        out = {}
        for c in self.cols:
            if c.storage == 0x50:
                pos = self.off + 8 + self.row_offset + i * self.row_width + c.row_pos
                out[c.name] = self._val(c.type, pos)[0]
            elif c.storage == 0x30:
                out[c.name] = c.const
            else:
                out[c.name] = None
        return out

    def set(self, buf, i, name, value):
        pos, t = self.cell_pos(i, name)
        struct.pack_into(TYPE_FMT[t], buf, pos, value)


# ---------------------------------------------------------------------------
# CPK / WESYS / Player.bin
# ---------------------------------------------------------------------------

def md5_file(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_wesys(buf: bytes, off: int):
    if buf[off:off + 5] != b"WESYS":
        raise ValueError(f"WESYS 헤더 없음 off={off}")
    packed, unpacked = struct.unpack_from("<II", buf, off + 5)
    raw = zlib.decompress(bytes(buf[off + 13:off + 13 + packed]))
    if len(raw) != unpacked:
        raise ValueError(f"압축 해제 크기 불일치 {len(raw)} != {unpacked}")
    return 13 + packed, raw


def to_wesys(raw: bytes) -> bytes:
    packed = zlib.compress(raw, 9)
    return b"WESYS" + struct.pack("<II", len(packed), len(raw)) + packed


def cpk_files(buf: bytes):
    hdr = UtfTable(buf, 16)
    h = hdr.row(0)
    toc = UtfTable(buf, h["TocOffset"] + 16)
    return hdr, h, toc, [toc.row(i) for i in range(toc.rows)]


def rebuild_cpk(old: bytes, new_player_file: bytes):
    hdr, h, toc, files = cpk_files(old)
    align, base = h["Align"], h["ContentOffset"]
    idx = next(i for i, f in enumerate(files) if f["FileName"] == "Player.bin")
    p = files[idx]
    start = base + p["FileOffset"]
    nxt = min((f["FileOffset"] for f in files if f["FileOffset"] > p["FileOffset"]), default=None)
    old_span = (base + nxt if nxt is not None else h["TocOffset"]) - start
    new_span = -(-len(new_player_file) // align) * align
    delta = new_span - old_span
    out = bytearray(old[:start]) + new_player_file + b"\x00" * (new_span - len(new_player_file)) + old[start + old_span:]
    hdr_n = UtfTable(out, 16)
    hdr_n.set(out, 0, "ContentSize", h["ContentSize"] + delta)
    hdr_n.set(out, 0, "TocOffset", h["TocOffset"] + delta)
    hdr_n.set(out, 0, "EtocOffset", h["EtocOffset"] + delta)
    size_diff = len(new_player_file) - p["FileSize"]
    hdr_n.set(out, 0, "EnabledPackedSize", h["EnabledPackedSize"] + size_diff)
    hdr_n.set(out, 0, "EnabledDataSize", h["EnabledDataSize"] + size_diff)
    toc_n = UtfTable(out, h["TocOffset"] + delta + 16)
    toc_n.set(out, idx, "FileSize", len(new_player_file))
    toc_n.set(out, idx, "ExtractSize", len(new_player_file))
    for i, f in enumerate(files):
        if f["FileOffset"] > p["FileOffset"]:
            toc_n.set(out, i, "FileOffset", f["FileOffset"] + delta)
    return bytes(out), delta, idx


def verify_cpk(old: bytes, new: bytes, idx: int, expect_raw: bytes):
    _, ho, _, fo = cpk_files(old)
    _, hn, _, fn = cpk_files(new)
    if len(fo) != len(fn):
        raise ValueError("파일 수 불일치")
    for i, (a, b) in enumerate(zip(fo, fn)):
        da = old[ho["ContentOffset"] + a["FileOffset"]:][: a["FileSize"]]
        db = new[hn["ContentOffset"] + b["FileOffset"]:][: b["FileSize"]]
        if i == idx:
            w = db.find(b"WESYS")
            _, raw = parse_wesys(db, w)
            if raw != expect_raw or db[:w] != da[:w]:
                raise ValueError("Player.bin 검증 실패")
        elif da != db or a["FileName"] != b["FileName"]:
            raise ValueError(f"파일 내용 변경됨 {b['FileName']}")
    if new[hn["TocOffset"]: hn["TocOffset"] + 4] != b"TOC " or new[hn["EtocOffset"]: hn["EtocOffset"] + 4] != b"ETOC":
        raise ValueError("TOC/ETOC 위치 검증 실패")


def fit_name(ko: str):
    b = ko.encode("utf-8")
    if len(b) < NAME_LEN:
        return ko, False
    toks = ko.split()
    while len(toks) > 1 and len(" ".join(toks).encode("utf-8")) >= NAME_LEN:
        toks.pop(1 if len(toks) > 2 else 0)
    out = " ".join(toks)
    while len(out.encode("utf-8")) >= NAME_LEN:
        out = out[:-1]
    return out, True


def build_player(raw: bytes, ko_map: dict[str, str]):
    buf = bytearray(raw)
    stats = {"patched": 0, "no_korean": 0, "trimmed": 0, "field251_diff": 0}
    n = len(buf) // RS
    for i in range(n):
        rec = i * RS
        pid = str(struct.unpack_from("<I", buf, rec + ID_OFF)[0])
        ko = ko_map.get(pid)
        if not ko:
            stats["no_korean"] += 1
            log.warning(
                "한글 이름 없음 id=%s name=%r",
                pid,
                bytes(buf[rec + NAME_OFF_68: rec + NAME_OFF_68 + NAME_LEN]).split(b"\x00")[0],
            )
            continue
        ko, trimmed = fit_name(ko)
        if trimmed:
            stats["trimmed"] += 1
            log.warning("이름 길이 초과로 축약 id=%s ko=%s", pid, ko)
        field = bytearray(NAME_LEN)
        kb = ko.encode("utf-8")
        field[: len(kb)] = kb
        en68 = bytes(buf[rec + NAME_OFF_68: rec + NAME_OFF_68 + NAME_LEN])
        en251 = bytes(buf[rec + NAME_OFF_251: rec + NAME_OFF_251 + NAME_LEN])
        buf[rec + NAME_OFF_68: rec + NAME_OFF_68 + NAME_LEN] = field
        if en251 == en68:
            buf[rec + NAME_OFF_251: rec + NAME_OFF_251 + NAME_LEN] = field
        else:
            stats["field251_diff"] += 1
            log.info("필드251이 표시명과 달라 유지 id=%s f251=%r", pid, en251.split(b"\x00")[0])
        stats["patched"] += 1
    return bytes(buf), stats


def load_csv(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.DictReader(fp))
    if not rows:
        raise ValueError(f"CSV 비어 있음 path={path}")
    if "id" not in rows[0] or "korean_name" not in rows[0]:
        raise ValueError("CSV에 id, korean_name 열이 필요합니다")
    bad = [r for r in rows if not HANGUL_ONLY.match((r.get("korean_name") or "").strip())]
    if bad:
        raise ValueError(f"한글 외 문자 {len(bad)}건 예={bad[:3]}")
    return {r["id"].strip(): r["korean_name"].strip() for r in rows}


def extract_player_blob(cpk: bytes):
    _, h, _, files = cpk_files(cpk)
    p = next(f for f in files if f["FileName"] == "Player.bin")
    start = h["ContentOffset"] + p["FileOffset"]
    blob = cpk[start: start + p["FileSize"]]
    w = blob.find(b"WESYS")
    if w < 0:
        raise ValueError("Player.bin 안에 WESYS 없음")
    _, raw = parse_wesys(blob, w)
    if len(raw) % RS != 0:
        raise ValueError(f"Player.bin raw 크기 이상 len={len(raw)} RS={RS}")
    return h, p, start, blob, w, raw


def build_hangul_cpk(input_cpk: bytes, ko_map: dict[str, str]):
    h, p, start, blob, w, raw = extract_player_blob(input_cpk)
    new_raw, stats = build_player(raw, ko_map)
    new_file = blob[:w] + to_wesys(new_raw)
    new_cpk, delta, idx = rebuild_cpk(input_cpk, new_file)
    verify_cpk(input_cpk, new_cpk, idx, new_raw)
    meta = {
        "player_old_size": p["FileSize"],
        "player_new_size": len(new_file),
        "cpk_old_size": len(input_cpk),
        "cpk_new_size": len(new_cpk),
        "delta": delta,
        "records": len(raw) // RS,
        "stats": stats,
        "wesys_off_in_player": w,
    }
    return new_cpk, meta


def parse_args(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="CSV로 data_s2627.cpk 한글화본 생성")
    ap.add_argument("--csv", type=Path, required=True, help="한글 명단 CSV")
    ap.add_argument("--input-cpk", type=Path, required=True, help="영문 기준 data_s2627.cpk")
    ap.add_argument("--output-cpk", type=Path, required=True, help="출력 한글 CPK")
    ap.add_argument("--dry-run", action="store_true", help="파일 쓰지 않고 검증만")
    ap.add_argument("--apply", type=Path, default=None, help="게임 CPK 경로에 백업 후 교체")
    ap.add_argument("--log", type=Path, default=None, help="로그 파일 경로")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_path = args.log
    if log_path is None:
        log_path = args.output_cpk.with_suffix(".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True)

    try:
        if not args.csv.is_file():
            log.error("CSV 없음 path=%s", args.csv)
            return 1
        if not args.input_cpk.is_file():
            log.error("입력 CPK 없음 path=%s", args.input_cpk)
            return 1

        ko_map = load_csv(args.csv)
        log.info("CSV 로드 id=%d path=%s", len(ko_map), args.csv)

        input_bytes = args.input_cpk.read_bytes()
        in_md5 = md5_file(args.input_cpk)
        log.info("입력 CPK size=%d md5=%s path=%s", len(input_bytes), in_md5, args.input_cpk)

        new_cpk, meta = build_hangul_cpk(input_bytes, ko_map)
        log.info(
            "빌드 완료 records=%d patched=%d missing=%d trimmed=%d field251_diff=%d "
            "Player %d->%d CPK %d->%d delta=%d",
            meta["records"],
            meta["stats"]["patched"],
            meta["stats"]["no_korean"],
            meta["stats"]["trimmed"],
            meta["stats"]["field251_diff"],
            meta["player_old_size"],
            meta["player_new_size"],
            meta["cpk_old_size"],
            meta["cpk_new_size"],
            meta["delta"],
        )
        if meta["stats"]["no_korean"]:
            log.error("CSV에 없는 선수 %d명, 중단", meta["stats"]["no_korean"])
            return 1

        if args.dry_run:
            log.info("드라이런 완료(파일 변경 없음) log=%s", log_path)
            return 0

        args.output_cpk.parent.mkdir(parents=True, exist_ok=True)
        args.output_cpk.write_bytes(new_cpk)
        out_md5 = md5_file(args.output_cpk)
        log.info("출력 저장 size=%d md5=%s path=%s", len(new_cpk), out_md5, args.output_cpk)

        if args.apply:
            game = args.apply
            if not game.is_file():
                log.error("적용 대상 게임 CPK 없음 path=%s", game)
                return 1
            game_md5 = md5_file(game)
            bak_dir = game.parent.parent / f"_backup_hangul_{time.strftime('%Y%m%d_%H%M%S')}"
            # prefer sibling backup under download's parent
            bak_dir = Path(str(game.parent)) / f"_backup_hangul_{time.strftime('%Y%m%d_%H%M%S')}"
            bak_dir.mkdir(parents=True, exist_ok=True)
            bak = bak_dir / game.name
            shutil.copy2(game, bak)
            if md5_file(bak) != game_md5:
                log.error("백업 검증 실패 bak=%s", bak)
                return 1
            shutil.copyfile(args.output_cpk, game)
            if md5_file(game) != out_md5:
                log.error("게임 CPK 교체 검증 실패, 백업에서 복구 필요 bak=%s", bak)
                return 1
            log.info("게임 적용 완료 game=%s md5=%s backup=%s", game, out_md5, bak)

        log.info("성공 output=%s", args.output_cpk)
        return 0
    except Exception:
        log.exception("CSV→한글 CPK 실패 csv=%s input=%s", args.csv, args.input_cpk)
        return 1


if __name__ == "__main__":
    sys.exit(main())
