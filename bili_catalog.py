"""Resolve public Bilibili video parts into download queue entries."""

from html.parser import HTMLParser
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zlib


SOURCE = "bilibili"
VIDEO_BASE = "https://www.bilibili.com/video/"
PAGELIST = "https://api.bilibili.com/x/player/pagelist"
_VIDEO_ID = re.compile(r"(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,18})")
_PAGE_NUMBER = re.compile(r"[1-9][0-9]{0,6}")
_VIDEO_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_SHORT_PATH = re.compile(r"/[A-Za-z0-9_-]{1,64}/?")
_SHORT_HOPS = 4
_SHORT_TIMEOUT = 10
_MAX_RESPONSE = 2 * 1024 * 1024
_MAX_PAGE_METADATA_BYTES = 256 * 1024
_INVALID_LINK = "请输入 B 站视频链接、b23.tv 短链、BV 号或 av 号。"
_SITE_SUFFIX = re.compile(r"(?:[_｜| \-]+)(?:哔哩哔哩|bilibili)(?:[_.｜| \-]+bilibili)?$", re.I)
_BOOK_TITLE = re.compile(r"《\s*([^《》]{1,120}?)\s*》")
_SOFT_TITLE = re.compile(r"【\s*([^【】]{1,120}?)\s*】|\[\s*([^\[\]]{1,120}?)\s*\]|「\s*([^「」]{1,120}?)\s*」")
_SONG_FIELDS = r"歌名|曲名|歌曲名(?:称|稱)?|song title"
_SONG_LABEL = re.compile(rf"(?:{_SONG_FIELDS})\s*[:：]\s*", re.I)
_PERFORMER_ROLES = ("翻唱", "演唱", "演唱者", "主唱", "歌手", "演奏", "演奏者",
                    "artist", "performed by", "vocal", "vocals", "原唱")
_PERFORMER_FIELDS = "|".join(_PERFORMER_ROLES)
_OTHER_CREDIT_FIELDS = (r"作[词詞]|填[词詞]|作曲|[编編]曲|[录錄]音|混音|字幕|剪[辑輯]|封面|素材|"
                        r"上[传傳]者?|投稿者?|UP主|[转轉][载載](?:自)?|[视視][频頻]後期|[视視][频頻]后期|"
                        r"后期|後期|BGM|composer|lyrics by|written by|arrangement|mixing")
_CREDIT_FIELDS = rf"{_PERFORMER_FIELDS}|{_OTHER_CREDIT_FIELDS}"
_CREDIT = re.compile(
    rf"(?<![A-Za-z])(?P<role>{_CREDIT_FIELDS})[^\S\r\n]*[:：][^\S\r\n]*"
    rf"(?P<name>[^\r\n\[\]【】〖〗（）()《》「」｜|丨，,；;]{{1,80}}?)"
    rf"(?=[^\S\r\n]*(?:$|[\r\n\[\]【】〖〗（）()《》「」｜|丨，,；;]|"
    rf"(?:[/／]\s*)?(?:{_CREDIT_FIELDS}|{_SONG_FIELDS})\s*[:：]))",
    re.I,
)
_VIDEO_QUALITY = (r"(?:真[.·\s]*)?(?:4K|8K|720P|1080P|1440P|2160P|4320P)"
                  r"(?:\s*@?\d{2,3}(?:FPS|P|帧))?(?:\s*(?:修复|重制|高清|珍藏)(?:版)?)?|"
                  r"\d{2,3}(?:FPS|帧)|HDR|HD")
_SONG_TAG = (r"[现現][场場](?:版|[纯純]享(?:版)?)?|[纯純]享(?:版)?|完整版|官方(?:版|MV)?|[录錄]音室(?:版)?|清唱(?:版)?|原唱(?:版)?|"
             r"翻唱(?:版)?|[无無][损損](?:音[质質])?|高音[质質]|超清(?:修[复復])?|歌[词詞]版|动态歌词|字幕版|"
             r"双语字幕|音[乐樂](?:[游遊][戏戲])?|音游|歌曲|"
             r"(?:原版|正式|官方)\s*MV|official(?: music| lyric)? video|HQ|SQ|Hi[ -]?Res|MV(?:版)?|MUSIC VIDEO|"
             r"lyrics?(?: video)?|cover(?: version)?|live(?: version)?|[粤粵][语語](?:版)?|[怀懷][旧舊]|[经經]典|"
             rf"{_VIDEO_QUALITY}")
_TAG_SEPARATOR = r"(?:\s*[/／|｜丨、,，+＋·]\s*|\s+)"
_TAG_SEQUENCE = rf"(?:{_SONG_TAG})(?:{_TAG_SEPARATOR}(?:{_SONG_TAG}))*"
_BRACKET_TAG = re.compile(rf"[\[【〖(（]\s*{_TAG_SEQUENCE}\s*[\]】〗)）]", re.I)
_LEADING_TAG = re.compile(rf"^(?:{_TAG_SEQUENCE})\s*[|｜丨·]\s*", re.I)
_EDGE_TAG = re.compile(rf"(?:\s*[|｜丨·/\-–—]\s*|\s+){_TAG_SEQUENCE}\s*$", re.I)
_ATTACHED_MV = re.compile(r"(?<=[\u3400-\u9fff])MV(?:版)?$", re.I)
_MUSIC_HINT = re.compile(r"演唱(?![会會])|翻唱|主唱|歌曲|音[乐樂]|原[声聲]|伴奏|主[题題]曲|[纯純]享|[无無][损損](?:音[质質])?|高音[质質]|"
                         r"歌[词詞]版|[录錄]音室(?:版)?|[现現][场場]版|神级现场|"
                         r"(?<![A-Za-z])(?:MV|OST|cover|live|lyrics|music video)(?![A-Za-z])", re.I)
_COVER_HINT = re.compile(r"翻唱|[弹彈]唱|[弹彈]奏版|改[编編]版|(?<![A-Za-z])cover(?: version)?(?![A-Za-z])", re.I)
_ENDING_ROLE = re.compile(r"\s*(演唱|原唱|翻唱|主唱)\s*$")
_NON_MUSIC = re.compile(r"解[说說]|[讲講]解|解析|看懂|[读讀]懂|影[评評]|[书書][评評]|[测測][评評]|[评評][测測]|[导導][读讀]|[书書]摘|"
                        r"剧情|[预預]告|片段|[观觀]后感|[读讀]后感|速看|教程|攻略|花絮|手元|"
                        r"[访訪][谈談]|[采採][访訪]|幕[后後]|"
                        r"鬼畜|[恶惡]搞|第\s*[0-9一二三四五六七八九十百]+\s*[章节集部期季]|\b(?:reaction|parody|gameplay)\b", re.I)
_COLLECTION = re.compile(r"合集|全集|串[烧燒]|歌[单單]|[选選]集|全收[录錄]|全\s*MV|"
                        r"[0-9一二三四五六七八九十百]+\s*(?:首|[Pp](?!\w))|"
                        r"\b(?:medley|playlist|compilation|music mix)\b", re.I)
_WORK_TITLE = re.compile(r"(?:收[录錄][于於]|出自|[来來]自|[专專][辑輯]|[电電]影|[电電][视視][剧劇]|[动動][画畫]|"
                         r"动漫|[游遊][戏戲]|原[声聲][带帶]|\bfrom)\s*[:：]?\s*$", re.I)
_WORK_LABEL = re.compile(r"^(?:[A-Z]\d+|\d+(?:P|首)|[游遊][戏戲]原[声聲]|原[声聲][带帶]|persona\s*[1-5][rgx]?)$|"
                         r"\b(?:OST|original soundtrack)\b|^(?:多|[一二三四五六七八九十\d]+)[种種][语語]言版$", re.I)
_PART_LABEL = re.compile(r"^(?:P\s*0*\d+|\d{1,2})\s*[.．、:：｜|丨]\s*(?=\S)", re.I)
_PART_PLACEHOLDER = re.compile(r"(?:P\s*0*\d+|未命名|正片|[单單]曲|[视視][频頻]\s*\d+|"
                               r"part\s+(?:\d+|one|two|three|four|five|six))", re.I)
_PROMOTION = re.compile(r"(?:我的(?:新歌|原[创創]|翻唱)|新歌首[发發]|[专專][辑輯](?:[试試][听聽]|[发發]布)|"
                        r"音[乐樂]分享|這是我寫的|这是我写的|快[来來][听聽]|\b(?:new song|out now|first cover)\b)", re.I)
_ARRANGEMENT = re.compile(r"(?:伴奏|人声|纯音乐|消音)(?:\s*[/／、]\s*(?:伴奏|人声|纯音乐|消音))*")
_ARTIST_SHAPE = re.compile(r"(?:[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7a3·]{1,12}|[A-Za-z0-9][A-Za-z0-9 .&'’_-]{0,39})")
_MIXED_ARTIST = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]{1,12}\s+[A-Za-z][A-Za-z .&'’_-]{0,39}|"
                           r"[A-Za-z][A-Za-z .&'’_-]{0,39}\s*[\u3040-\u30ff\u3400-\u9fff]{1,12}")
_ARTIST_EXCLUSION = re.compile(r"上线|发布|即将|版本|新版|重制版|预览|试听|视频|原著|原作|小说|电影|动画|动漫|"
                               r"合集|全篇|完整|一口气|有声书|热点|音源|背景|封面|投稿|剪辑|转载|字幕|官方|演唱会|"
                               r"演唱|翻唱|原唱|神级|同人|一起|分享|高音质|无损|高清|经典|一首|音乐|歌曲|[语語]言版|"
                               r"才是|这是|這是|真的|必须听|一定要|听哭|聽哭|好听|好聽|搬运|搬運|UP主|"
                               r"\b(?:full|movie|trailer|episode|audiobook|official|cover|feat|ost|soundtrack)\b", re.I)
_DASH = re.compile(r"\s+-+\s+|\s*[–—]+\s*|\s*-+\s*(?=[\u3400-\u9fff])")
_MUSIC_CATEGORIES = frozenset(("音乐", "音樂", "原创音乐", "翻唱", "演奏", "音乐现场", "音乐综合", "music"))
_CLOSE_BRACKET = {"[": "]", "【": "】", "〖": "〗", "(": ")", "（": "）"}
_FOLDER_NON_MUSIC = re.compile(
    r"解[说說]|[讲講]解|影[评評]|[实實][况況]|[实實][机機](?:演示|玩法)|攻略|教程|教[学學]|[评評][测測]|"
    r"通[关關]|全[连連]|手元|[谱譜]面(?:展示|[预預][览覽]|[预預]告|演示)|[预預]告片|配音|"
    r"(?<![A-Za-z])(?:gameplay|full combo|all perfect|reaction)(?![A-Za-z])",
    re.I,
)
_FOLDER_MUSIC = re.compile(
    r"歌曲|金曲|[粤粵][语語]歌(?![词詞])|(?:[经經]典|[怀懷][旧舊])老歌|老歌合集|"
    r"音[乐樂](?:合集|集|欣[赏賞]|分享|全集|[选選]集|全收集)|[纯純]享|演唱(?![会會])|翻唱|演奏|"
    r"(?<![A-Za-z])(?:MV|music collection|music video)(?![A-Za-z])",
    re.I,
)
_FOLDER_SOUNDTRACK = re.compile(r"原[声聲](?:[带帶]|音[乐樂])?|(?<![A-Za-z])(?:OST|original soundtrack)(?![A-Za-z])", re.I)
_FOLDER_BGM = re.compile(r"(?<![A-Za-z])BGM(?![A-Za-z])", re.I)
_FOLDER_PERSONA = re.compile(
    r"女神[异異][闻聞][录錄]|ペルソナ(?:[1-5]|シリーズ)|"
    r"(?<![A-Za-z0-9])(?:persona[ -]*(?:[1-5](?:fes|[rgx])?|series)|p5x)(?![A-Za-z0-9])",
    re.I,
)
_FOLDER_RHYTHM = re.compile(
    r"音游|音[乐樂][游遊][戏戲]|(?<![A-Za-z0-9])(?:phigros|arcaea|maimai|chunithm|muse dash|"
    r"sound voltex|project sekai|beatmania(?: iidx)?)(?![A-Za-z0-9])",
    re.I,
)
_FOLDER_RHYTHM_AUDIO = re.compile(r"音游(?:曲(?:目)?|原[声聲]|配[乐樂])|音[乐樂][游遊][戏戲](?:原[声聲]|配[乐樂])")
_FOLDER_GAME_AUDIO = re.compile(r"[游遊][戏戲](?:原[声聲]|配[乐樂]|主[题題]曲)")
_FOLDER_OTHER_SOUNDTRACK = re.compile(r"(?:电影|電視|电视|影片|动画|動畫|动漫|動漫)(?:版|的)?\s*(?:原[声聲]|配[乐樂]|OST)", re.I)
_FOLDER_GAME_PARTITIONS = frozenset(("游戏", "遊戲", "单机游戏", "单机游戏区", "网络游戏", "电子竞技", "音游", "音乐游戏"))
_FOLDER_CANTONESE = re.compile(
    r"[\[【〖(（]\s*[粤粵][语語]\s*[\]】〗)）]|"
    r"[粤粵][语語](?:歌(?![词詞])|金曲|[经經]典|老歌|翻唱|原唱|演唱|音[乐樂]|流行|[现現][场場]版|版)"
)
_FOLDER_OLDIES = re.compile(r"老歌|[怀懷][旧舊](?:金曲|歌曲)|(?:上世[纪紀]|[5-9]0年代)(?:金曲|歌曲)")


class BilibiliCatalogError(RuntimeError):
    """A public video cannot be resolved."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


class _VideoMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.og_title = ""
        self.author = ""
        self.title = []
        self.in_title = False
        self.script = None
        self.video = {}
        self.breadcrumbs = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            attributes = dict(attrs)
            if attributes.get("property") == "og:title" and not self.og_title:
                self.og_title = attributes.get("content") or ""
            elif attributes.get("name") == "author" and not self.author:
                self.author = attributes.get("content") or ""
        elif tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self.script = []

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "script" and self.script is not None:
            try:
                data = json.loads("".join(self.script))
            except (ValueError, RecursionError):
                data = None
            self.script = None
            if not isinstance(data, dict):
                return
            if data.get("@type") == "VideoObject" and not self.video:
                self.video = data
            elif data.get("@type") == "BreadcrumbList" and isinstance(data.get("itemListElement"), list):
                self.breadcrumbs = data["itemListElement"]

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif self.script is not None:
            self.script.append(data)


def _display_title(value):
    return " ".join(value.split())[:220] if isinstance(value, str) else ""


def _breadcrumb_category(items):
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("item"), str):
            continue
        try:
            target = urlsplit(item["item"])
        except ValueError:
            continue
        if (target.scheme == "https" and target.netloc in _VIDEO_HOSTS
                and re.fullmatch(r"/(?:c|v)/[a-z0-9_/-]+/?", target.path)):
            if name := _display_title(item.get("name")):
                return name
    return ""


def _extract_video_metadata(html):
    parser = _VideoMetadataParser()
    parser.feed(html.split("</head>", 1)[0])
    title = _display_title(parser.video.get("name")) or parser.og_title or "".join(parser.title)
    title = _display_title(_SITE_SUFFIX.sub("", title))
    unavailable = ("视频去哪儿了", "视频去哪了", "视频不见了", "访问受限", "页面不存在")
    if not title or title.lower() in ("哔哩哔哩", "bilibili") or title.startswith(unavailable):
        return {}

    author = parser.video.get("author")
    names = [author] if isinstance(author, dict) else author if isinstance(author, list) else []
    uploaders = dict.fromkeys(name for item in names if isinstance(item, dict)
                              and (name := _display_title(item.get("name"))))
    uploader = _display_title("、".join(uploaders) or parser.author)

    genres = parser.video.get("genre")
    if isinstance(genres, list):
        genres = next((_display_title(value) for value in genres if _display_title(value)), "")
    category = _display_title(genres) or _breadcrumb_category(parser.breadcrumbs)
    description = parser.video.get("description")
    return {"title": title, "uploader": uploader, "source_category": category,
            "description": description[:1600] if isinstance(description, str) else ""}


def _fetch_video_metadata(video_id):
    request = Request(f"{VIDEO_BASE}{video_id}/", headers={
        "Accept": "text/html", "Accept-Encoding": "gzip, identity", "User-Agent": "Mozilla/5.0",
    })
    try:
        with build_opener(_NoRedirect()).open(request, timeout=4) as response:
            body = response.read(_MAX_PAGE_METADATA_BYTES)
            encoding = response.headers.get("Content-Encoding", "identity").lower()
        if encoding == "gzip":
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = decompressor.decompress(body, _MAX_PAGE_METADATA_BYTES)
        elif encoding != "identity":
            return {}
        return _extract_video_metadata(body.decode("utf-8", "replace"))
    except HTTPError as error:
        error.close()
        return {}
    except (OSError, HTTPException, ValueError, zlib.error):
        return {}


def _trim_parts(title):
    title = _display_title(_BRACKET_TAG.sub(" ", title))
    title = _LEADING_TAG.sub("", title)
    title = _EDGE_TAG.sub("", title).strip(" |｜丨·:/／,，;；-–—")
    return _ATTACHED_MV.sub("", title)


def _artist_name(value):
    name = _display_title(value).strip(" @＠|｜丨·:/／,，;；-–—")
    if _ARTIST_EXCLUSION.search(name) or re.fullmatch(_TAG_SEQUENCE, name, re.I) or name.isdecimal():
        return ""
    names = re.split(r"\s*[/／&＆+＋×、]\s*", name)
    return name if all(_ARTIST_SHAPE.fullmatch(item) or _MIXED_ARTIST.fullmatch(item) for item in names) else ""


def _nearby_artist(text, is_cover):
    text = text.strip(" |｜丨·:/／,，;；-–—")
    role = _ENDING_ROLE.search(text)
    if role and (not is_cover or role[1] != "原唱"):
        return _artist_name(text[:role.start()])
    return "" if is_cover else _artist_name(text)


def _title_matches(text, pattern):
    matches = []
    previous = 0
    for match in pattern.finditer(text):
        lead = text[previous:match.start()]
        track = next(value for value in match.groups() if value is not None)
        after = text[match.end():]
        is_work = _WORK_TITLE.search(lead) or re.match(r"\s*(?:原声带|原聲帶|专辑|專輯|OST\b)", after, re.I)
        if not is_work and not (_ARRANGEMENT.fullmatch(track) or _WORK_LABEL.search(track)
                                or _COLLECTION.search(track) or _NON_MUSIC.search(track)):
            matches.append((match, track))
        previous = match.end()
    return matches


def _performer(credits, is_cover):
    for role in _PERFORMER_ROLES:
        if role == "原唱" and is_cover:
            continue
        artists = dict.fromkeys(name for credit in credits if credit["role"].casefold() == role
                                and (name := _artist_name(credit["name"])))
        if artists:
            return next(iter(artists)) if len(artists) == 1 else ""
    return ""


def _is_non_music(text):
    return bool(_NON_MUSIC.search(text) or _FOLDER_NON_MUSIC.search(text))


def _track_name(value):
    track = _trim_parts(value)
    if (not track or _ARRANGEMENT.fullmatch(track) or _PART_PLACEHOLDER.fullmatch(track)
            or re.fullmatch(_TAG_SEQUENCE, track, re.I)):
        return ""
    return track


def _song_metadata(title, source_category="", *, part=False, description=""):
    """Recognize song-title patterns while leaving ordinary video titles untouched."""
    original = _display_title(title)
    music_category = _display_title(source_category).casefold() in _MUSIC_CATEGORIES
    credits = tuple(_CREDIT.finditer(original))
    description_credits = tuple(_CREDIT.finditer(description[:1600]))
    is_cover = bool(_COVER_HINT.search(original) or
                    any(credit["role"].casefold() == "翻唱" for credit in description_credits))
    artist = _performer(credits, is_cover) or _performer(description_credits, is_cover)
    without_credits = _CREDIT.sub("", original)
    without_credits = re.sub(r"\[\s*\]|【\s*】|〖\s*〗|\(\s*\)|（\s*）|「\s*」", " ", without_credits)
    candidate = _trim_parts(without_credits)
    if part or music_category:
        candidate = _PART_LABEL.sub("", candidate)
    has_music_hint = _MUSIC_HINT.search(original) is not None
    labelled = _SONG_LABEL.search(candidate)
    if labelled:
        lead = candidate[:labelled.start()].rstrip()
        if _is_non_music(lead) or _COLLECTION.search(lead):
            return "", ""
        candidate = _trim_parts(candidate[labelled.end():])
        if closing := _CLOSE_BRACKET.get(lead[-1:]):
            candidate = candidate.partition(closing)[0]
        matches = _title_matches(candidate, _BOOK_TITLE) or _title_matches(candidate, _SOFT_TITLE)
        if len(matches) > 1:
            return "", ""
        if matches and matches[0][0].start() == 0:
            return _track_name(matches[0][1]), artist
        segment = re.split(r"[|｜丨；;]", candidate, maxsplit=1)[0]
        track = "" if _is_non_music(segment) or _COLLECTION.search(segment) else _track_name(segment)
        return track, artist if track else ""

    matches = _title_matches(candidate, _BOOK_TITLE)
    if not matches and not _BOOK_TITLE.search(candidate) and (music_category or has_music_hint or artist):
        matches = _title_matches(candidate, _SOFT_TITLE)
    if len(matches) == 1:
        (title_match, track), = matches
        before, after = candidate[:title_match.start()].strip(), candidate[title_match.end():].strip()
        context = f"{before} {after}"
        if _is_non_music(context) or _COLLECTION.search(context):
            return "", ""
        artist_lead = re.split(r"\s+[–—-]\s+|[|｜丨·]", before.strip(" |｜丨·-/–—"))[-1]
        leading_artist = _nearby_artist(artist_lead, is_cover) if len(artist_lead) <= 55 else ""
        trailing_artist = _nearby_artist(after, is_cover) if re.match(r"^[-–—|｜丨·]", after) else ""
        if not artist and (music_category or has_music_hint or not source_category):
            artist = leading_artist or trailing_artist
        if artist or has_music_hint or music_category:
            return _track_name(track), artist
        return "", ""
    if (matches or _is_non_music(candidate) or _COLLECTION.search(candidate)
            or not (music_category or has_music_hint or artist)):
        return "", ""

    if _BOOK_TITLE.search(candidate) or _SOFT_TITLE.search(candidate):
        return "", ""
    parts = _DASH.split(candidate, maxsplit=1)
    if len(parts) == 2 and (leading_artist := _artist_name(parts[0])):
        if track := _track_name(parts[1]):
            return track, artist or ("" if is_cover else leading_artist)
    if _PROMOTION.search(candidate):
        return "", ""
    if (candidate != original or artist or (part and music_category and len(candidate) <= 100)):
        if track := _track_name(candidate):
            return track, artist
    return "", ""


def clean_song_title(title):
    return _song_metadata(title)[0]


def _media_category(source_category, title, audio_title):
    if audio_title or (_display_title(source_category).casefold() in _MUSIC_CATEGORIES and not _is_non_music(title)):
        return "music"
    if source_category or _is_non_music(title):
        return "video"
    return ""


def classify_title(title, source_category="", *, part=False):
    """Return provisional metadata available from a Bilibili title alone."""
    title = _display_title(title)
    audio_title, artist = _song_metadata(title, source_category, part=part)
    return {"audio_title": audio_title or None, "artist": artist if audio_title else "",
            "media_category": _media_category(source_category, title, audio_title)}


def suggest_folder(title, source_category, media_category, *, parent_title=""):
    """Suggest one music folder from a Bilibili title or the title of its upload."""
    title = _display_title(title)
    if _FOLDER_NON_MUSIC.search(_BOOK_TITLE.sub("", title)):
        return ""
    parent_title = _display_title(parent_title)
    if parent_title != title and not _FOLDER_NON_MUSIC.search(_BOOK_TITLE.sub("", parent_title)):
        title = f"{title} {parent_title}".strip()

    partition = _display_title(source_category).casefold()
    soundtrack = _FOLDER_SOUNDTRACK.search(title)
    music = media_category == "music" or partition in _MUSIC_CATEGORIES or _FOLDER_MUSIC.search(title)
    if _FOLDER_PERSONA.search(title) and (music or soundtrack or _FOLDER_BGM.search(title)):
        return "Persona 系列"
    if _FOLDER_RHYTHM_AUDIO.search(title) or (_FOLDER_RHYTHM.search(title) and (music or soundtrack)):
        return "音游"
    if _FOLDER_GAME_AUDIO.search(title) or (partition in _FOLDER_GAME_PARTITIONS and soundtrack
                                           and not _FOLDER_OTHER_SOUNDTRACK.search(title)):
        return "游戏原声"
    if music and _FOLDER_CANTONESE.search(title):
        return "粤语"
    if music and _FOLDER_OLDIES.search(title):
        return "老歌"
    return ""


def _clean_link(value):
    if not isinstance(value, str):
        raise ValueError(_INVALID_LINK)
    value = value.strip()
    if len(value) > 4096 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError(_INVALID_LINK)
    return value


def parse_video_link(value):
    """Return a bare or full video link's ID and optional part without a request."""
    value = _clean_link(value)
    if _VIDEO_ID.fullmatch(value):
        return value, None

    try:
        url = urlsplit(value)
    except ValueError:
        raise ValueError(_INVALID_LINK) from None
    if url.scheme != "https" or url.netloc.lower() not in _VIDEO_HOSTS:
        raise ValueError(_INVALID_LINK)
    path = re.fullmatch(r"/video/([^/]+)/?", url.path)
    if not path or not _VIDEO_ID.fullmatch(path[1]):
        raise ValueError(_INVALID_LINK)

    pages = parse_qs(url.query, keep_blank_values=True).get("p", [])
    if pages and (len(pages) != 1 or not _PAGE_NUMBER.fullmatch(pages[0])):
        raise ValueError("B 站视频的分 P 编号无效。")
    return path[1], int(pages[0]) if pages else None


def _short_link(value):
    try:
        url = urlsplit(_clean_link(value))
    except ValueError:
        return None
    if url.scheme == "https" and url.netloc.lower() == "b23.tv" and _SHORT_PATH.fullmatch(url.path):
        return url._replace(netloc="b23.tv", path=url.path.rstrip("/"), fragment="").geturl()
    return None


def is_video_link(value):
    """Recognize supported video or short link syntax without a request."""
    try:
        parse_video_link(value)
        return True
    except ValueError:
        return _short_link(value) is not None


def _short_destination(url, timeout):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout):
            raise BilibiliCatalogError("该 B 站短链没有返回视频地址。")
    except HTTPError as error:
        try:
            locations = error.headers.get_all("Location", []) if error.headers else []
            if error.code in (301, 302, 303, 307, 308) and len(locations) == 1 and locations[0]:
                return locations[0]
        finally:
            error.close()
        raise BilibiliCatalogError("该 B 站短链已失效或无法访问。") from None
    except (URLError, OSError):
        raise BilibiliCatalogError("无法连接 B 站短链，请检查网络后重试。") from None


def _expand_short_link(url):
    deadline = time.monotonic() + _SHORT_TIMEOUT
    seen = set()
    for _ in range(_SHORT_HOPS):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or url in seen:
            break
        seen.add(url)
        location = _short_destination(url, remaining)
        try:
            location = _clean_link(location)
            if "\\" in location or location.startswith("///"):
                raise ValueError(_INVALID_LINK)
            destination = urljoin(url, location)
            follow = _short_link(destination)
            if follow is None:
                if urlsplit(destination).netloc.lower() != "www.bilibili.com":
                    raise ValueError(_INVALID_LINK)
                return parse_video_link(destination)
        except ValueError:
            raise BilibiliCatalogError("该 B 站短链没有指向受支持的视频页。") from None
        url = follow
    raise BilibiliCatalogError("该 B 站短链重定向过多或解析超时。")


def _fetch_parts(video_id, session=None):
    if video_id.startswith("BV"):
        query = {"bvid": video_id}
    else:
        query = {"aid": video_id[2:]}
    headers = {
        "Accept": "application/json",
        "Referer": "https://www.bilibili.com/",
        "User-Agent": "Mozilla/5.0",
    }
    if session is not None:
        headers["Cookie"] = session.cookie_header()
    request = Request(f"{PAGELIST}?{urlencode(query)}", headers=headers)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            content = response.read(_MAX_RESPONSE + 1)
    except HTTPError as error:
        error.close()
        if error.code in (401, 403, 404, 410, 412):
            raise BilibiliCatalogError("B 站视频暂时不可访问，请在浏览器中确认链接。") from None
        raise BilibiliCatalogError("B 站暂时无法返回视频信息，请稍后重试。") from None
    except (URLError, OSError):
        raise BilibiliCatalogError("无法连接 B 站，请检查网络后重试。") from None

    if len(content) > _MAX_RESPONSE:
        raise BilibiliCatalogError("B 站返回的视频信息过大。")
    try:
        result = json.loads(content)
    except (UnicodeError, ValueError):
        raise BilibiliCatalogError("B 站返回的视频信息无法读取。") from None
    if not isinstance(result, dict) or type(result.get("code")) is not int:
        raise BilibiliCatalogError("B 站返回的视频信息无法读取。")
    if result["code"] != 0:
        raise BilibiliCatalogError("B 站视频暂时不可访问，请在浏览器中确认链接。")
    return result.get("data")


def resolve_video(value, *, metadata=True, session=None):
    """Resolve every public part, or the single part explicitly named in the URL."""
    short = _short_link(value)
    video_id, selected_page = _expand_short_link(short) if short else parse_video_link(value)
    parts = _fetch_parts(video_id) if session is None else _fetch_parts(video_id, session)
    if not isinstance(parts, list) or not parts:
        raise BilibiliCatalogError("未找到可解析的 B 站视频分 P。")
    if len(parts) > 10_000:
        raise BilibiliCatalogError("B 站视频分 P 数量过多。")
    if selected_page is not None and not any(isinstance(part, dict) and part.get("page") == selected_page for part in parts):
        raise BilibiliCatalogError(f"该视频没有 P{selected_page}。")
    info = _fetch_video_metadata(video_id) if metadata else {}
    video_title = info.get("title", "")
    uploader = info.get("uploader", "")
    source_category = info.get("source_category", "")
    description = info.get("description", "")
    multipart = len(parts) > 1

    rows = {}
    for item in parts:
        if not isinstance(item, dict):
            raise BilibiliCatalogError("B 站返回的视频分 P 信息无效。")
        position, cid = item.get("page"), item.get("cid")
        if (type(position) is not int or not 1 <= position <= 9_999_999
                or position in rows or type(cid) is not int or cid <= 0):
            raise BilibiliCatalogError("B 站返回的视频分 P 信息无效。")
        part_title = _display_title(item.get("part"))
        duration = item.get("duration")
        title = (video_title if not multipart else "") or part_title or f"P{position}"
        song_source = title
        audio_title = artist = media_category = ""
        if metadata:
            matching_part = (video_title == part_title and bool(part_title)
                             and type(duration) is int and 45 <= duration <= 720)
            can_use_plain_title = multipart or matching_part
            audio_title, artist = _song_metadata(title, source_category, part=can_use_plain_title)
            if (not audio_title and part_title and title != part_title
                    and not _is_non_music(title) and not _COLLECTION.search(title)):
                song_source = part_title
                audio_title, artist = _song_metadata(part_title, source_category)
            if audio_title and description and not multipart:
                audio_title, artist = _song_metadata(song_source, source_category, part=can_use_plain_title,
                                                     description=description)
            if not audio_title:
                artist = ""
                if not info:
                    audio_title = None
            media_category = _media_category(source_category, song_source, audio_title)
        rows[position] = {
            "source": SOURCE,
            "url": f"{VIDEO_BASE}{video_id}?p={position}",
            "cid": cid,
            "id": video_id,
            "position": position,
            "total": len(parts),
            "title": title,
            "audio_title": audio_title,
            "artist": artist,
            "uploader": uploader,
            "source_category": source_category,
            "media_category": media_category,
            "duration": duration if type(duration) is int and duration >= 0 else 0,
            "batch_key": f"bilibili:{video_id}",
        }

    title = video_title or (next(iter(rows.values()))["title"] if len(rows) == 1 else video_id)
    positions = [selected_page] if selected_page is not None else sorted(rows)
    return {
        "id": video_id,
        "title": title,
        "uploader": uploader,
        "source_category": source_category,
        "media_category": classify_title(title, source_category)["media_category"] if metadata else "",
        "rows": [dict(rows[position], batch_title=title) for position in positions],
    }
