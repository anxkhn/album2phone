import base64
import hashlib

from album2phone import album_token, plan


def master(rn, name, size, created, w=4032, h=3024, clip=None):
    fields = {
        "filenameEnc": {"value": base64.b64encode(name.encode()).decode()},
        "resOriginalRes": {"value": {"size": size, "downloadURL": f"https://x/{rn}/${{f}}?k=1"}},
        "originalCreationDate": {"value": created},
        "resOriginalWidth": {"value": w},
        "resOriginalHeight": {"value": h},
    }
    if clip:
        fields["resOriginalVidComplRes"] = {"value": {"size": clip, "downloadURL": f"https://x/{rn}c/${{f}}"}}
    return {"recordType": "CPLMaster", "recordName": rn, "fields": fields}


def asset(rn, date, deleted=0):
    return {
        "recordType": "CPLAsset",
        "fields": {
            "masterRef": {"value": {"recordName": rn}},
            "assetDate": {"value": date},
            "isDeleted": {"value": deleted},
        },
    }


def test_album_token():
    assert (
        album_token("https://photos.icloud.com/shared/album/0aBcDeFgHiJkLmNoPqRsTuV_w")
        == "0aBcDeFgHiJkLmNoPqRsTuV_w"
    )
    assert (
        album_token("https://www.icloud.com/photos/#/sc,0aBcDeFgHiJkLmNoPqRsTuV_w/")
        == "0aBcDeFgHiJkLmNoPqRsTuV_w"
    )
    assert album_token("0aBcDeFgHiJkLmNoPqRsTuV_w") == "0aBcDeFgHiJkLmNoPqRsTuV_w"


def test_plan():
    records = [
        master("a", "IMG_1.HEIC", 1000, 1_000_000, clip=50),
        master("b", "IMG_1.HEIC", 1200, 2_000_000),  # other phone, same name: keep, rename
        master("c", "CLIP.MP4", 5000, 3_000_000),
        master("d", "CLIP.MP4", 5000, 9_000_000),  # same video uploaded again: drop
        master("e", "IMG_2.HEIC", 100_000, 4_000_000),
        master("f", "IMG_2.HEIC", 100_040, 4_000_000),  # re-saved copy, few bytes more: drop
        master("g", "GONE.JPG", 10, 5_000_000),  # only a deleted asset points here
    ]
    records += [asset(k, 7_000_000) for k in "abcdef"] + [asset("a", 8_000_000), asset("g", 1, deleted=1)]

    files, dups = plan(records, "/sdcard/DCIM/Trip")
    paths = [f["path"] for f in files]
    assert dups == 2
    assert paths == [
        "/sdcard/DCIM/Trip/IMG_1.HEIC",
        "/sdcard/DCIM/Trip/Live Photo clips/IMG_1.MOV",
        "/sdcard/DCIM/Trip/IMG_1_" + hashlib.sha1(b"b").hexdigest()[:6] + ".HEIC",
        "/sdcard/DCIM/Trip/CLIP.MP4",
        "/sdcard/DCIM/Trip/IMG_2.HEIC",
    ]
    assert files[0]["url"] == "https://x/a/IMG_1.HEIC?k=1" and files[0]["epoch"] == 7000

    files, dups = plan(records, "/d", live=False, dedupe=False)
    assert dups == 0 and len(files) == 6 and not any("Live Photo" in f["path"] for f in files)
