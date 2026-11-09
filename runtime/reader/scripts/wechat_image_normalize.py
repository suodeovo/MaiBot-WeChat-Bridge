"""Bounded local image previews shared by received and collected stickers."""
import base64,hashlib,io
import itertools,pathlib,re,subprocess,tempfile,time
from PIL import Image,ImageOps


def decode_image_source(raw, timeout=5):
    """Decode a single full-size WXGF photo without the sticker contact sheet."""
    if raw[:4].lower() != b'wxgf':
        return raw
    if not 0 < len(raw) <= 32 * 1024 * 1024:
        raise RuntimeError('IMAGE_TOO_LARGE_OR_EMPTY')
    # Some containers include a conventional image instead of a raw HEVC stream.
    for match in re.finditer(b'\xff\xd8\xff|\x89PNG\r\n\x1a\n', raw[4:4096]):
        candidate = raw[4 + match.start():]
        try:
            with Image.open(io.BytesIO(candidate)) as image:
                if image.width * image.height <= 40_000_000:
                    image.verify()
                    return candidate
        except (OSError, ValueError):
            continue
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise RuntimeError('IMAGE_FFMPEG_UNAVAILABLE') from None
    # WXGF may contain metadata between HEVC NAL units, or separate preview
    # and full-image streams. Reuse the bundled reader's VPS grouping approach.
    starts = list(itertools.islice(re.finditer(b'\x00\x00\x00?\x01', raw[4:]),4097))
    if len(starts) > 4096:
        raise RuntimeError('IMAGE_WXGF_DECODE_FAILED')
    units = []
    for index, match in enumerate(starts):
        end = 4 + starts[index + 1].start() if index + 1 < len(starts) else len(raw)
        unit = raw[4 + match.end():end]
        if len(unit) >= 2 and not unit[0] & 0x80:
            units.append(unit)
    vps = [index for index, unit in enumerate(units) if ((unit[0] >> 1) & 63) == 32]
    groups = [units[start:vps[index+1] if index+1 < len(vps) else len(units)] for index,start in enumerate(vps)]
    streams = [b''.join(b'\x00\x00\x00\x01' + unit for unit in group) for group in groups
        if any(((unit[0] >> 1) & 63) in (1,19,20) for unit in group)]
    streams.sort(key=len, reverse=True)
    groups_count = len(streams)
    streams.extend([b''.join(b'\x00\x00\x00\x01' + unit for unit in units), raw[4:]])
    deadline = time.monotonic() + timeout
    best, best_area = None, 0
    with tempfile.TemporaryDirectory(prefix='wechatagent-photo-') as directory:
        source, target = pathlib.Path(directory) / 'input.hevc', pathlib.Path(directory) / 'frame.png'
        for index, stream in enumerate(streams):
            if best is not None and index >= groups_count:
                break
            remaining = deadline - time.monotonic()
            if not stream or remaining <= .05:
                continue
            source.write_bytes(stream)
            if target.exists():
                target.unlink()
            try:
                result = subprocess.run([executable,'-hide_banner','-loglevel','error','-nostdin','-y',
                    '-max_alloc','134217728','-threads','1','-f','hevc','-i',str(source),'-frames:v','1',
                    '-vf',r'scale=w=min(2048\,iw):h=min(2048\,ih):force_original_aspect_ratio=decrease',str(target)],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=remaining,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if result.returncode == 0 and target.is_file() and target.stat().st_size <= 32 * 1024 * 1024:
                    with Image.open(target) as image:
                        area = image.width * image.height
                    if best_area < area <= 40_000_000:
                        best, best_area = target.read_bytes(), area
            except subprocess.TimeoutExpired:
                break
    if best is not None:
        return best
    raise RuntimeError('IMAGE_WXGF_DECODE_FAILED')

def normalize_image(raw, animated=False, *, max_side=1280, max_source_bytes=8 * 1024 * 1024):
    if not raw or len(raw) > max_source_bytes:
        raise RuntimeError('IMAGE_TOO_LARGE_OR_EMPTY')
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 40_000_000:
            raise RuntimeError('IMAGE_PIXEL_LIMIT')
        if animated and getattr(source, 'is_animated', False):
            # Bound decoded pixels and frames. A static sheet conveys sampled
            # animation content without repeatedly sending entire animations.
            budget = min(128, max(1, 80_000_000 // (source.width * source.height)))
            count=min(getattr(source,'n_frames',1),budget)
            frames=[]
            for index in dict.fromkeys((0,(count-1)//2,count-1)):
                source.seek(index)
                frame = source.convert('RGBA')
                frame.thumbnail((384, 384))
                background = Image.new('RGB', frame.size, 'white')
                background.paste(frame, mask=frame.getchannel('A'))
                frames.append(background)
            image = Image.new('RGB', (sum(f.width for f in frames), max(f.height for f in frames)), 'white')
            x = 0
            for frame in frames:
                image.paste(frame, (x, 0)); x += frame.width
        else:
            image = ImageOps.exif_transpose(source).convert('RGB')
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        # Keep small text legible when possible, while retaining the existing
        # transport cap. Detailed photos may need another bounded resize.
        for _ in range(8):
            for quality in (85, 75, 65):
                output = io.BytesIO()
                image.save(output, format='JPEG', quality=quality, optimize=True)
                data = output.getvalue()
                if len(data) <= 768 * 1024:
                    break
            if len(data) <= 768 * 1024:
                break
            image.thumbnail((max(1, int(image.width * .8)), max(1, int(image.height * .8))), Image.Resampling.LANCZOS)
        else:
            raise RuntimeError('NORMALIZED_IMAGE_TOO_LARGE')
        return {'b64': base64.b64encode(data).decode('ascii'), 'mime': 'image/jpeg',
            'width': image.width, 'height': image.height, 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest()}

