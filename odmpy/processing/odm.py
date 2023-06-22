# Copyright (C) 2023 github.com/ping
#
# This file is part of odmpy.
#
# odmpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# odmpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with odmpy.  If not, see <http://www.gnu.org/licenses/>.
#

import argparse
import base64
import datetime
import hashlib
import json
import logging
import math
import re
import shutil
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict
from functools import reduce
from html import unescape as unescape_html
from pathlib import Path
from typing import Any, Union, Dict, List, Optional

from mutagen.id3 import ID3, CTOC, CTOCFlags, TIT2, CHAP, Encoding
from mutagen.mp3 import MP3, BitrateMode
from requests.exceptions import HTTPError, ConnectionError
from termcolor import colored
from tqdm import tqdm

from .shared import (
    generate_names,
    write_tags,
    generate_cover,
    remux_mp3,
    merge_into_mp3,
    convert_to_m4b,
    create_opf,
    init_session,
    update_chapters,
    FfmpegChapterMarker,
    Tag,
)
from ..cli_utils import OdmpyCommands
from ..constants import OMC, OS, UA, UNSUPPORTED_PARSER_ENTITIES, UA_LONG
from ..errors import OdmpyRuntimeError
from ..libby import USER_AGENT
from ..overdrive import OverDriveClient
from ..utils import (
    slugify,
    parse_duration_to_seconds,
    parse_duration_to_milliseconds,
    get_element_text,
    plural_or_singular_noun as ps,
)

RESERVE_ID_RE = re.compile(
    r"(?P<reserve_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

#
# Main processing logic for odm-based downloads
#


def _patch_for_parse_error(text: str) -> str:
    # [TODO]: Find a more generic solution instead of patching entities, maybe lxml?
    # Ref: https://github.com/ping/odmpy/issues/19
    return "<!DOCTYPE xml [{patch}]>{text}".format(
        patch="".join(
            [
                f'<!ENTITY {entity} "{replacement}">'
                for entity, replacement in UNSUPPORTED_PARSER_ENTITIES.items()
            ]
        ),
        text=text,
    )


def process_odm(
    odm_file: Optional[Path],
    loan: Dict,
    args: argparse.Namespace,
    logger: logging.Logger,
    cleanup_odm_license: bool = False,
) -> None:
    """
    Download the audiobook loan using the specified odm file

    :param odm_file:
    :param loan:
    :param args:
    :param logger:
    :param cleanup_odm_license:
    :return:
    """
    if not odm_file:
        logger.warning("No odm file specified.")
        return

    ffmpeg_loglevel = "info" if logger.level == logging.DEBUG else "fatal"
    xml_doc = ET.parse(odm_file)
    root = xml_doc.getroot()
    overdrive_media_id = root.attrib.get("id", "")
    metadata = None
    for t in root.itertext():
        if not t.startswith("<Metadata>"):
            continue
        # remove invalid '&' char
        text = re.sub(r"\s&\s", " &amp; ", t)
        try:
            metadata = ET.fromstring(text)
        except ET.ParseError:
            metadata = ET.fromstring(_patch_for_parse_error(text))
        break

    if not metadata:
        raise ValueError("Unable to find Metadata in ODM")

    title = get_element_text(metadata.find("Title"))
    sub_title = get_element_text(metadata.find("SubTitle"))
    publisher = get_element_text(metadata.find("Publisher"))
    description = get_element_text(metadata.find("Description"))
    series = get_element_text(metadata.find("Series"))
    cover_url = get_element_text(metadata.find("CoverUrl"))
    authors = [
        unescape_html(get_element_text(c))
        for c in metadata.find("Creators") or []
        if "Author" in c.attrib.get("role", "")
    ]
    if not authors:
        authors = [
            unescape_html(get_element_text(c))
            for c in metadata.find("Creators") or []
            if "Editor" in c.attrib.get("role", "")
        ]
    if not authors:
        authors = [
            unescape_html(get_element_text(c))
            for c in metadata.find("Creators") or []
            if c.text
        ]
    narrators = [
        unescape_html(get_element_text(c))
        for c in metadata.find("Creators") or []
        if "Narrator" in c.attrib.get("role", "")
    ]
    languages = [
        lang.attrib.get("code", "")
        for lang in metadata.find("Languages") or []
        if lang.attrib.get("code", "")
    ]
    subjects = [subj.text for subj in metadata.find("Subjects") or [] if subj.text]

    debug_meta: Dict[str, Any] = {
        "meta": {
            "title": title,
            "coverUrl": cover_url,
            "authors": authors,
            "publisher": publisher,
            "description": description,
        }
    }

    # View Book Info
    if args.command_name == OdmpyCommands.Information:
        if args.format == "text":
            logger.info(f'{"Title:":10} {colored(title, "blue")}')
            logger.info(
                "{:10} {}".format(
                    "Creators:",
                    colored(
                        ", ".join(
                            [
                                f"{c.text} ({c.attrib['role']})"
                                for c in metadata.find("Creators") or []
                            ]
                        ),
                        "blue",
                    ),
                )
            )
            logger.info(f"{'Publisher:':10} {publisher}")
            logger.info(f"{'Subjects:':10} {', '.join(subjects)}")
            logger.info(
                f"{'Languages:':10} {', '.join([c.text for c in metadata.find('Languages') or [] if c.text])}"
            )
            logger.info(f"{'Description:':10}\n{description}")
            for formats in root.findall("Formats"):
                for f in formats:
                    logger.info(f"\n{'Format:':10} {f.attrib['name']}")
                    parts = f.find("Parts") or []
                    for p in parts:
                        logger.info(
                            f"* {p.attrib['name']} - {p.attrib['duration']} ({math.ceil(1.0 * int(p.attrib['filesize']) / 1024):,.0f}kB)"
                        )

        elif args.format == "json":
            result: Dict[str, Any] = {
                "title": title,
                "creators": [
                    f"{c.text} ({c.attrib['role']})"
                    for c in metadata.find("Creators") or []
                ],
                "publisher": publisher,
                "subjects": [c.text for c in metadata.find("Subjects") or [] if c.text],
                "languages": [
                    c.text for c in metadata.find("Languages") or [] if c.text
                ],
                "description": description,
                "formats": [],
            }

            for formats in root.findall("Formats"):
                for f in formats:
                    parts = []
                    total_secs = 0
                    for p in f.find("Parts") or []:
                        part_duration = p.attrib["duration"]
                        # part duration can look like '%M:%S.%f' or '%H:%M:%S.%f'
                        total_secs = parse_duration_to_seconds(part_duration)
                        parts.append(
                            {
                                "name": p.attrib["name"],
                                "duration": part_duration,
                                "filesize": f"{math.ceil(1.0 * int(p.attrib['filesize']) / 1024):,.0f}kB",
                            }
                        )
                    result["formats"].append(
                        {"format": f.attrib["name"], "parts": parts}
                    )
                    # in case there are multiple formats, only need to store it once
                    if "total_duration" not in result:
                        result["total_duration"] = {
                            "total_minutes": round(total_secs / 60),
                            "total_seconds": round(total_secs),
                        }

            logger.info(json.dumps(result))

        return

    session = init_session(max_retries=args.retries)

    # Download Book
    download_baseurl = ""
    download_parts = []
    for formats in root.findall("Formats"):
        for f in formats:
            protocols = f.find("Protocols") or []
            for p in protocols:
                if p.attrib.get("method", "") != "download":
                    continue
                download_baseurl = p.attrib["baseurl"]
                break
            parts = f.find("Parts") or []
            for p in parts:
                download_parts.append(p.attrib)
    debug_meta["download_parts"] = download_parts

    logger.info(
        f'Downloading "{colored(title, "blue", attrs=["bold"])}" '
        f'by "{colored(", ".join(authors), "blue", attrs=["bold"])}" '
        f'in {len(download_parts)} {ps(len(download_parts), "part")}...'
    )

    book_folder, book_filename = generate_names(
        title=title,
        series=series,
        series_reading_order=loan.get("detailedSeries", {}).get("readingOrder", ""),
        authors=authors,
        edition="",
        title_id=loan.get("id") or overdrive_media_id,
        args=args,
        logger=logger,
    )
    book_m4b_filename = book_filename.with_suffix(".m4b")

    # check early if a merged file is already saved
    if (
        args.merge_output
        and (
            book_filename if args.merge_format == "mp3" else book_m4b_filename
        ).exists()
    ):
        logger.warning(
            'Already saved "%s"',
            colored(
                str(book_filename if args.merge_format == "mp3" else book_m4b_filename),
                "magenta",
            ),
        )
        if cleanup_odm_license and odm_file.exists():
            try:
                odm_file.unlink()
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f'Error deleting "{odm_file}": {str(e)}')
        return

    debug_filename = book_folder.joinpath("debug.json")

    cover_filename, cover_bytes = generate_cover(
        book_folder=book_folder,
        cover_url=cover_url,
        session=session,
        timeout=args.timeout,
        logger=logger,
    )

    license_ele = root.find("License")
    if license_ele is None:
        raise ValueError("Unable to find License in ODM")

    acquisition_url = get_element_text(license_ele.find("AcquisitionUrl"))
    if not acquisition_url:
        raise ValueError("Unable to extract acquisition_url from ODM")

    media_id = root.attrib["id"]

    client_id = str(uuid.uuid1()).upper()
    raw_hash = f"{client_id}|{OMC}|{OS}|ELOSNOC*AIDEM*EVIRDREVO"
    m = hashlib.sha1(raw_hash.encode("utf-16-le"))
    license_hash = base64.b64encode(m.digest())

    # Extract license:
    # License file is downloadable only once per odm,
    # so we keep it in case downloads fail
    license_file = Path(args.download_dir, odm_file.with_suffix(".license").name)
    if license_file.exists():
        logger.warning(f"Already downloaded license file: {license_file}")
    else:
        # download license file
        params = OrderedDict(
            [
                ("MediaID", media_id),
                ("ClientID", client_id),
                ("OMC", OMC),
                ("OS", OS),
                ("Hash", license_hash),
            ]
        )

        license_res = session.get(
            acquisition_url,
            params=params,
            headers={"User-Agent": UA},
            timeout=args.timeout,
            stream=True,
        )
        try:
            license_res.raise_for_status()
            with license_file.open("wb") as outfile:
                for chunk in license_res.iter_content(1024):
                    outfile.write(chunk)
            logger.debug(f"Saved license file {license_file}")

        except HTTPError as he:
            if he.response.status_code == 404:
                # odm file has expired
                logger.error(
                    f'The loan file "{args.odm_file}" has expired. Please download again.'
                )
            else:
                logger.error(he.response.content)
            raise OdmpyRuntimeError("HTTP Error while downloading license.")
        except ConnectionError as ce:
            logger.error(f"ConnectionError: {str(ce)}")
            raise OdmpyRuntimeError("Connection Error while downloading license.")

    license_xml_doc = ET.parse(license_file)
    license_root = license_xml_doc.getroot()

    ns = "{http://license.overdrive.com/2008/03/License.xsd}"

    signed_info_ele = license_root.find(f"{ns}SignedInfo")
    if signed_info_ele is None:
        raise ValueError("Unable to find SignedInfo in License")

    license_client_id = get_element_text(signed_info_ele.find(f"{ns}ClientID"))
    if not license_client_id:
        raise ValueError("Unable to find ClientID in License.SignedInfo")

    with license_file.open("r", encoding="utf-8") as lic_file:
        lic_file_contents = lic_file.read()

    track_count = 0
    file_tracks: List[Dict] = []
    keep_cover = args.always_keep_cover
    audio_lengths_ms = []
    audio_bitrate = 0
    for p in download_parts:
        part_number = int(p["number"])
        part_filename = book_folder.joinpath(
            f"{slugify(f'{title} - Part {part_number:02d}', allow_unicode=True)}.mp3"
        )
        part_tmp_filename = part_filename.with_suffix(".part")
        part_file_size = int(p["filesize"])
        part_url_filename = p["filename"]
        part_download_url = f"{download_baseurl}/{part_url_filename}"
        part_markers = []

        if part_filename.exists():
            logger.warning("Already saved %s", colored(str(part_filename), "magenta"))
        else:
            try:
                already_downloaded_len = 0
                if part_tmp_filename.exists():
                    already_downloaded_len = part_tmp_filename.stat().st_size

                part_download_res = session.get(
                    part_download_url,
                    headers={
                        "User-Agent": UA,
                        "ClientID": license_client_id,
                        "License": lic_file_contents,
                        "Range": f"bytes={already_downloaded_len}-"
                        if already_downloaded_len
                        else None,
                    },
                    timeout=args.timeout,
                    stream=True,
                )
                part_download_res.raise_for_status()

                with tqdm.wrapattr(
                    part_download_res.raw,
                    "read",
                    total=part_file_size,
                    initial=already_downloaded_len,
                    desc=f"Part {part_number:2d}",
                    disable=args.hide_progress,
                ) as res_raw:
                    with part_tmp_filename.open(
                        "ab" if already_downloaded_len else "wb"
                    ) as outfile:
                        shutil.copyfileobj(res_raw, outfile)

                # try to remux file to remove mp3 lame tag errors
                remux_mp3(
                    part_tmp_filename=part_tmp_filename,
                    part_filename=part_filename,
                    ffmpeg_loglevel=ffmpeg_loglevel,
                    logger=logger,
                )

            except HTTPError as he:
                logger.error(f"HTTPError: {str(he)}")
                logger.debug(he.response.content)
                raise OdmpyRuntimeError("HTTP Error while downloading part file.")

            except ConnectionError as ce:
                logger.error(f"ConnectionError: {str(ce)}")
                raise OdmpyRuntimeError("Connection Error while downloading part file.")

            # Save id3 info only on new download, ref #42
            # This also makes handling of part files consistent with merged files
            try:
                # Fill id3 info for mp3 part
                audio_file = MP3(part_filename, ID3=ID3)

                if audio_file.info.bitrate_mode == BitrateMode.CBR:
                    audio_bitrate = int(audio_file.info.bitrate / 1000)

                write_tags(
                    audiofile=audio_file,
                    title=title,
                    sub_title=sub_title,
                    authors=authors,
                    narrators=narrators,
                    publisher=publisher,
                    description=description,
                    cover_bytes=cover_bytes,
                    genres=subjects,
                    languages=languages,
                    published_date=None,  # odm does not contain date info
                    series=series,
                    part_number=part_number,
                    total_parts=len(download_parts),
                    overdrive_id=overdrive_media_id,
                    always_overwrite=args.overwrite_tags,
                    delimiter=args.tag_delimiter,
                )
                audio_file.save(v2_version=args.id3v2_version)

                audio_lengths_ms.append(int(round(audio_file.info.length * 1000)))

                # Extract OD chapter info from mp3s for use in merged file
                if (
                    "TXXX:OverDrive MediaMarkers" in audio_file.tags
                    and audio_file.tags["TXXX:OverDrive MediaMarkers"].text
                ):
                    frame_text = re.sub(
                        r"\s&\s",
                        " &amp; ",
                        audio_file.tags["TXXX:OverDrive MediaMarkers"].text[0],
                    )
                    try:
                        tree = ET.fromstring(frame_text)
                    except UnicodeEncodeError:
                        tree = ET.fromstring(
                            frame_text.encode("ascii", "ignore").decode("ascii")
                        )
                    except ET.ParseError:
                        tree = ET.fromstring(_patch_for_parse_error(frame_text))
                    for marker in tree.iter("Marker"):  # type: ET.Element
                        marker_name = get_element_text(marker.find("Name")).strip()
                        marker_timestamp = get_element_text(marker.find("Time"))

                        # 2 timestamp formats found ("%M:%S.%f", "%H:%M:%S.%f")
                        ts_mark = parse_duration_to_milliseconds(marker_timestamp)
                        track_count += 1
                        part_markers.append(
                            (f"ch{track_count:02d}", marker_name, ts_mark)
                        )

                if (
                    args.add_chapters
                    and not args.merge_output
                    and (
                        args.overwrite_tags
                        or Tag.TableOfContents not in audio_file.tags
                    )
                ):
                    # set the chapter marks
                    generated_markers: List[Dict[str, Union[str, int]]] = []
                    for j, file_marker in enumerate(part_markers):
                        generated_markers.append(
                            {
                                "id": file_marker[0],
                                "text": file_marker[1],
                                "start_time": int(file_marker[2]),
                                "end_time": int(
                                    round(audio_file.info.length * 1000)
                                    if j == (len(part_markers) - 1)
                                    else part_markers[j + 1][2]
                                ),
                            }
                        )

                    if args.overwrite_tags and Tag.TableOfContents in audio_file.tags:
                        # Clear existing toc
                        audio_file.tags.pop(Tag.TableOfContents)

                    # We can't use update_chapters here because it requires ffmpeg,
                    # and we only specify the ffmpeg requirement for merging

                    audio_file.tags.add(
                        CTOC(
                            element_id="toc",
                            flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                            child_element_ids=[m["id"] for m in generated_markers],
                            sub_frames=[
                                TIT2(encoding=Encoding.UTF8, text=["Table of Contents"])
                            ],
                        )
                    )

                    for gm in generated_markers:
                        audio_file.tags.add(
                            CHAP(
                                element_id=gm["id"],
                                start_time=gm["start_time"],
                                end_time=gm["end_time"],
                                sub_frames=[
                                    TIT2(encoding=Encoding.UTF8, text=[gm["text"]])
                                ],
                            )
                        )
                        start_time = datetime.timedelta(
                            milliseconds=float(gm["start_time"])
                        )
                        end_time = datetime.timedelta(
                            milliseconds=float(gm["end_time"])
                        )
                        logger.debug(
                            'Added chap tag => %s: %s-%s "%s" to "%s"',
                            colored(str(gm["id"]), "cyan"),
                            start_time,
                            end_time,
                            colored(str(gm["text"]), "cyan"),
                            colored(str(part_filename), "blue"),
                        )
                    audio_file.save(v2_version=args.id3v2_version)

            except Exception as e:  # pylint: disable=broad-except
                logger.warning(
                    "Error saving ID3: %s", colored(str(e), "red", attrs=["bold"])
                )
                keep_cover = True

            logger.info('Saved "%s"', colored(str(part_filename), "magenta"))

        file_tracks.append(
            {
                "file": part_filename,
                "markers": part_markers,
            }
        )
    # end loop: for p in download_parts:

    debug_meta["audio_lengths_ms"] = audio_lengths_ms
    debug_meta["file_tracks"] = [
        {"file": str(f["file"]), "markers": f["markers"]} for f in file_tracks
    ]

    if args.merge_output:
        logger.info(
            'Generating "%s"...',
            colored(
                str(book_filename if args.merge_format == "mp3" else book_m4b_filename),
                "magenta",
            ),
        )

        merge_into_mp3(
            book_filename=book_filename,
            file_tracks=file_tracks,
            audio_bitrate=audio_bitrate,
            ffmpeg_loglevel=ffmpeg_loglevel,
            hide_progress=args.hide_progress,
            logger=logger,
        )

        audio_file = MP3(book_filename, ID3=ID3)
        if not audio_file.tags:
            audio_file.tags = ID3()
        write_tags(
            audiofile=audio_file,
            title=title,
            sub_title=sub_title,
            authors=authors,
            narrators=narrators,
            publisher=publisher,
            description=description,
            cover_bytes=cover_bytes,
            genres=subjects,
            languages=languages,
            published_date=None,  # odm does not contain date info
            series=series,
            part_number=0,
            total_parts=0,
            overdrive_id=overdrive_media_id,
            overwrite_title=True,
            always_overwrite=args.overwrite_tags,
            delimiter=args.tag_delimiter,
        )
        audio_file.save(v2_version=args.id3v2_version)

        if args.add_chapters and (
            args.overwrite_tags or Tag.TableOfContents not in audio_file.tags
        ):
            merged_markers: List[Dict[str, Union[str, int]]] = []
            for i, f in enumerate(file_tracks):
                prev_tracks_len_ms = (
                    0 if i == 0 else reduce(lambda x, y: x + y, audio_lengths_ms[0:i])
                )
                this_track_endtime_ms = int(
                    reduce(lambda x, y: x + y, audio_lengths_ms[0 : i + 1])
                )
                file_markers = f["markers"]
                for j, file_marker in enumerate(file_markers):
                    merged_markers.append(
                        {
                            "id": file_marker[0],
                            "text": str(file_marker[1]),
                            "start_time": int(file_marker[2]) + prev_tracks_len_ms,
                            "end_time": int(
                                this_track_endtime_ms
                                if j == (len(file_markers) - 1)
                                else file_markers[j + 1][2] + prev_tracks_len_ms
                            ),
                        }
                    )
            debug_meta["merged_markers"] = merged_markers

            if args.overwrite_tags and Tag.TableOfContents in audio_file.tags:
                # Clear existing toc to prevent "There may only be one top-level table of contents.
                audio_file.pop(Tag.TableOfContents)
                audio_file.save(v2_version=args.id3v2_version)

            update_chapters(
                target_filepath=book_filename,
                chapters=[
                    FfmpegChapterMarker(
                        title=mm["text"],  # type: ignore[arg-type]
                        start_millisecond=mm["start_time"],  # type: ignore[arg-type]
                        end_millisecond=mm["end_time"],  # type: ignore[arg-type]
                    )
                    for mm in merged_markers
                ],
                output_format="mp3",
                ffmpeg_loglevel=ffmpeg_loglevel,
                logger=logger,
            )

        if args.merge_format == "mp3":
            logger.info(
                'Merged files into "%s"',
                colored(
                    str(
                        book_filename
                        if args.merge_format == "mp3"
                        else book_m4b_filename
                    ),
                    "magenta",
                ),
            )

        if args.merge_format == "m4b":
            convert_to_m4b(
                book_filename=book_filename,
                book_m4b_filename=book_m4b_filename,
                cover_filename=cover_filename,
                merge_codec=args.merge_codec,
                audio_bitrate=audio_bitrate,
                ffmpeg_loglevel=ffmpeg_loglevel,
                hide_progress=args.hide_progress,
                logger=logger,
            )

        if not args.keep_mp3:
            for f in file_tracks:
                try:
                    f["file"].unlink()
                except Exception as e:  # pylint: disable=broad-except
                    logger.warning(f'Error deleting "{f["file"]}": {str(e)}')

    if cleanup_odm_license:
        for target_file in [odm_file, license_file]:
            if target_file and target_file.exists():
                try:
                    target_file.unlink()
                except Exception as e:  # pylint: disable=broad-except
                    logger.warning(f'Error deleting "{target_file}": {str(e)}')

    if not keep_cover and cover_filename.exists():
        try:
            cover_filename.unlink()
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Error deleting "{cover_filename}": {str(e)}')

    if args.generate_opf:
        if args.merge_output:
            opf_file_path = book_filename.with_suffix(".opf")
        else:
            opf_file_path = book_folder.joinpath(
                f"{slugify(title, allow_unicode=True)}.opf"
            )

        if not opf_file_path.exists():
            mobj = RESERVE_ID_RE.match(overdrive_media_id)
            if not mobj:
                logger.warning(
                    f"Could not get a valid reserve ID: {overdrive_media_id}"
                )
            else:
                reserve_id = mobj.group("reserve_id")
                od_client = OverDriveClient(
                    user_agent=USER_AGENT, timeout=args.timeout, retry=args.retries
                )
                media_info = od_client.media(reserve_id)
                create_opf(
                    media_info,
                    cover_filename if keep_cover else None,
                    file_tracks
                    if not args.merge_output
                    else [
                        {
                            "file": book_filename
                            if args.merge_format == "mp3"
                            else book_m4b_filename
                        }
                    ],
                    opf_file_path,
                    logger,
                )
        else:
            logger.info("Already saved %s", colored(str(opf_file_path), "magenta"))

    if args.write_json:
        with debug_filename.open("w", encoding="utf-8") as outfile:
            json.dump(debug_meta, outfile, indent=2)


def process_odm_return(args: argparse.Namespace, logger: logging.Logger) -> None:
    """
    Return the audiobook loan using the specified odm file

    :param logger:
    :param args:
    :return:
    """
    xml_doc = ET.parse(args.odm_file)
    root = xml_doc.getroot()

    logger.info(f"Returning {args.odm_file} ...")
    early_return_url = get_element_text(root.find("EarlyReturnURL"))
    if not early_return_url:
        raise OdmpyRuntimeError("Unable to get EarlyReturnURL")
    sess = init_session(args.retries)
    try:
        early_return_res = sess.get(
            early_return_url, headers={"User-Agent": UA_LONG}, timeout=args.timeout
        )
        early_return_res.raise_for_status()
        logger.info(f"Loan returned successfully: {args.odm_file}")
    except HTTPError as he:
        if he.response.status_code == 403:
            logger.warning("Loan is probably already returned.")
            return
        logger.error(f"HTTPError: {str(he)}")
        logger.debug(he.response.content)
        raise OdmpyRuntimeError(f"HTTP error returning odm {args.odm_file, }")
    except ConnectionError as ce:
        logger.error(f"ConnectionError: {str(ce)}")
        raise OdmpyRuntimeError(f"Connection error returning odm {args.odm_file, }")
