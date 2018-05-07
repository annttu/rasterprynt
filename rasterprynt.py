import contextlib
import logging
import socket
import struct

try:
    from urllib.request import urlopen
except ImportError:  # Python 2
    from urllib2 import urlopen
try:
    from urllib.error import URLError
except ImportError:  # Python 2
    from urllib2 import URLError


logger = logging.getLogger('printer')

# All 18mm. Size of a stripe (height in Brother-talk)
STRIPE_SIZE = {
    'P950NW': 408,
    '9800PCN': 312,
}
STRIPE_SIZE_DEFAULT = STRIPE_SIZE['P950NW']


# Cache of IP address -> model name
_PRINTER_BY_IP = {}


def detect_printer_model(ip):
    cached = _PRINTER_BY_IP.get(ip)
    if cached:
        return cached

    try:
        res = _detect_printer_model_uncached(ip)
    except URLError as urle:
        logging.warning('Failed to detect printer at %s: %s' % (ip, urle))
        return 'error'
    if res:
        _PRINTER_BY_IP[ip] = res
    return res


def _detect_printer_model_uncached(ip):
    # We use /admin/default.html because this seems to be the only common URL for both supported printers so far
    test_url = 'http://%s/admin/default.html' % ip
    try:
        with contextlib.closing(urlopen(test_url)) as url_handle:
            html = url_handle.read()
    except URLError as urle:
        if hasattr(urle, 'code') and urle.code == 401:
            html = urle.read()
        else:
            raise

    if b'<TITLE>Brother PT-9800PCN</TITLE>' in html:
        return '9800PCN'
    if b'<title>Brother PT-P950NW</title>' in html:
        return 'P950NW'

    return None


# Compress a row of bytes according to Brother's TIFF standard.
# Yields byte that make up the compressed row.
# See page 34 & 36 at http://download.brother.com/welcome/docp000771/cv_pth500p700e500_eng_raster_110.pdf
def _compress_tiff(row):
    pos = 0
    uncompressed_start = pos
    while pos < len(row):
        count = 0
        while pos + count + 1 < len(row) and row[pos + count + 1] == row[pos + count]:
            count += 1

        if count > 0:
            # Flush uncompressed buffer
            if uncompressed_start < pos:
                yield struct.pack('!b', pos - uncompressed_start - 1) + row[uncompressed_start:pos]

            # Output the compressed tag
            yield struct.pack('!bB', -count, row[pos])
            pos += count + 1
            uncompressed_start = pos
        else:
            # Uncompressed buffer continues
            pos += 1

    # Flush remaining uncompressed buffer
    if uncompressed_start < pos:
        yield struct.pack('!b', pos - uncompressed_start - 1) + row[uncompressed_start:pos]


# Scan a line from the image and yield the bytes that make them
def _raw_row(img, img_bytes, stripe_count, x, y_offset):
    for stripe_idx in range(stripe_count):
        bits = 0
        for bit_index in range(8):
            y = stripe_idx * 8 + bit_index - y_offset
            if x < img.width and 0 <= y < img.height:
                color = img_bytes[(x, y)]
                if isinstance(color, int):  # grayscale
                    px = color
                else:  # RGB
                    px = sum(color) / 3
                bits |= (0 if px > 230 else 1) << (7 - bit_index)
        yield struct.pack('!B', bits)


def _empty_row(stripe_count, use_tiff):
    if use_tiff:
        compressed = b''.join(_compress_tiff(b'\x00' * stripe_count))
        assert compressed == struct.pack('!bB', 1 - stripe_count, 0)
        return (b'G' + struct.pack('<H', len(compressed)) + compressed)
    else:
        return (b'G' + struct.pack('<H', stripe_count) + (b'\x00' * stripe_count))


def render(images, ip=None, printer_model=None, top_margin=8, bottom_margin=8):
    # Yields bytes that can be printed on a Brother P950NW(new printer) or Brother 9800PCN(old printer).
    # The protocol here is reverse-engineered from what the Windows driver for brother printers sends.
    # Many commands are documented at
    #  http://download.brother.com/welcome/docp000771/cv_pth500p700e500_eng_raster_110.pdf
    # The ESC/P command reference at
    #  http://support.brother.com/g/b/manuallist.aspx?c=us&lang=en&prod=p950nweus&flang=English&type3=384&type2=81
    # can also help.
    # Our old code and brother sends 200 0-bytes here (maybe to synchronize the serial bus? No need for that via TCP)

    USE_TIFF = False

    if printer_model is None:
        printer_model = detect_printer_model(ip) if ip else None

    # number of dots in a stripe (depends on printer + tape size)
    stripe_size = STRIPE_SIZE.get(printer_model, STRIPE_SIZE_DEFAULT)
    assert stripe_size % 8 == 0
    stripe_count = stripe_size // 8

    yield b'\x1b@'  # Init
    yield b'\x1bia\x01'  # Raster mode

    yield b'\x1biM\x00'  # Various Mode settings: no auto cut
    yield b'\x1bid\x00\x00'  # Margin = 0

    first = True
    for img in images:
        if first:
            first = False
        else:
            yield b'\x0c'

        img_bytes = img.load()

        # The "raster number" seems to be the width, or length of the stripe
        raster_number = img.width + top_margin + bottom_margin

        yield (
            b'\x1biz'  # Print information command
            b'\xc0' +  # PI_RECOVER | PI_QUALITY
            b'\x00' +  # Media type: not set
            b'\x00' +  # Media width, e.g. 18 = 18mm. We're setting it to 0 (unspecified)
            b'\x00' +  # Media length: not set
            struct.pack('<I', raster_number) +  # "Raster number"
            (b'\x00' if first else b'\x01') +  # Starting page?
            b'\x00')   # This byte is always 0 (reserved)

        if USE_TIFF:
            yield b'\x4d\x02'  # Select compression mode: TIFF
        else:
            yield b'\x4d\x00'  # Select compression mode: Simple

        # Draw margin.
        # For compatibility with different printers, we send empty lines instead of specifying a margin.
        yield _empty_row(stripe_count, USE_TIFF) * top_margin

        for x in range(img.width):
            offset = (stripe_size - img.height)

            row = b''.join(_raw_row(img, img_bytes, stripe_count, x, offset))
            assert len(row) == stripe_count
            if USE_TIFF:
                compressed_row = b''.join(_compress_tiff(row))
                yield b'G' + struct.pack('<H', len(compressed_row))
                yield compressed_row
            else:
                yield b'G' + struct.pack('<H', len(row))
                yield row

        # Draw bottom margin.
        # For compatibility with different printers, we send empty lines instead of specifying a margin.
        yield _empty_row(stripe_count, USE_TIFF) * top_margin

    yield b'\x1a'  # Print


def cat(images, ip=None, top_margin=8, bottom_margin=8):
    return b''.join(
        render(images, ip, top_margin=top_margin, bottom_margin=bottom_margin))


def print(images, ip, top_margin=8, bottom_margin=8):
    data = cat(images, ip, top_margin, bottom_margin)
    sock = socket.create_connection((ip, 9100))
    sock.sendall(data)
    sock.close()