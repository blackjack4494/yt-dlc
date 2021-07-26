from __future__ import division, unicode_literals

import os
import time
import json

try:
    from Crypto.Cipher import AES
    can_decrypt_frag = True
except ImportError:
    can_decrypt_frag = False

try:
    import concurrent.futures
    can_threaded_download = True
except ImportError:
    can_threaded_download = False

from .common import FileDownloader
from .http import HttpFD
from ..compat import (
    compat_urllib_error,
    compat_struct_pack,
)
from ..utils import (
    DownloadError,
    error_to_compat_str,
    encodeFilename,
    sanitize_open,
    sanitized_Request,
)


class HttpQuietDownloader(HttpFD):
    def to_screen(self, *args, **kargs):
        pass


class FragmentFD(FileDownloader):
    """
    A base file downloader class for fragmented media (e.g. f4m/m3u8 manifests).

    Available options:

    fragment_retries:   Number of times to retry a fragment for HTTP error (DASH
                        and hlsnative only)
    skip_unavailable_fragments:
                        Skip unavailable fragments (DASH and hlsnative only)
    keep_fragments:     Keep downloaded fragments on disk after downloading is
                        finished
    _no_ytdl_file:      Don't use .ytdl file

    For each incomplete fragment download yt-dlp keeps on disk a special
    bookkeeping file with download state and metadata (in future such files will
    be used for any incomplete download handled by yt-dlp). This file is
    used to properly handle resuming, check download file consistency and detect
    potential errors. The file has a .ytdl extension and represents a standard
    JSON file of the following format:

    extractor:
        Dictionary of extractor related data. TBD.

    downloader:
        Dictionary of downloader related data. May contain following data:
            current_fragment:
                Dictionary with current (being downloaded) fragment data:
                index:  0-based index of current fragment among all fragments
            fragment_count:
                Total count of fragments

    This feature is experimental and file format may change in future.
    """

    def report_retry_fragment(self, err, frag_index, count, retries):
        self.to_screen(
            '\r[download] Got server HTTP error: %s. Retrying fragment %d (attempt %d of %s) ...'
            % (error_to_compat_str(err), frag_index, count, self.format_retries(retries)))

    def report_skip_fragment(self, frag_index):
        self.to_screen('[download] Skipping fragment %d ...' % frag_index)

    def _prepare_url(self, info_dict, url):
        headers = info_dict.get('http_headers')
        return sanitized_Request(url, None, headers) if headers else url

    def _prepare_and_start_frag_download(self, ctx, info_dict):
        self._prepare_frag_download(ctx)
        self._start_frag_download(ctx, info_dict)

    def __do_ytdl_file(self, ctx):
        return not ctx['live'] and not ctx['tmpfilename'] == '-' and not self.params.get('_no_ytdl_file')

    def _read_ytdl_file(self, ctx):
        assert 'ytdl_corrupt' not in ctx
        stream, _ = sanitize_open(self.ytdl_filename(ctx['filename']), 'r')
        try:
            ytdl_data = json.loads(stream.read())
            ctx['fragment_index'] = ytdl_data['downloader']['current_fragment']['index']
            if 'extra_state' in ytdl_data['downloader']:
                ctx['extra_state'] = ytdl_data['downloader']['extra_state']
        except Exception:
            ctx['ytdl_corrupt'] = True
        finally:
            stream.close()

    def _write_ytdl_file(self, ctx):
        frag_index_stream, _ = sanitize_open(self.ytdl_filename(ctx['filename']), 'w')
        downloader = {
            'current_fragment': {
                'index': ctx['fragment_index'],
            },
        }
        if 'extra_state' in ctx:
            downloader['extra_state'] = ctx['extra_state']
        if ctx.get('fragment_count') is not None:
            downloader['fragment_count'] = ctx['fragment_count']
        frag_index_stream.write(json.dumps({'downloader': downloader}))
        frag_index_stream.close()

    def _download_fragment(self, ctx, frag_url, info_dict, headers=None, request_data=None):
        fragment_filename = '%s-Frag%d' % (ctx['tmpfilename'], ctx['fragment_index'])
        fragment_info_dict = {
            'url': frag_url,
            'http_headers': headers or info_dict.get('http_headers'),
            'request_data': request_data,
        }
        success = ctx['dl'].download(fragment_filename, fragment_info_dict)
        if not success:
            return False, None
        if fragment_info_dict.get('filetime'):
            ctx['fragment_filetime'] = fragment_info_dict.get('filetime')
        ctx['fragment_filename_sanitized'] = fragment_filename
        return True, self._read_fragment(ctx)

    def _read_fragment(self, ctx):
        down, frag_sanitized = sanitize_open(ctx['fragment_filename_sanitized'], 'rb')
        ctx['fragment_filename_sanitized'] = frag_sanitized
        frag_content = down.read()
        down.close()
        return frag_content

    def _append_fragment(self, ctx, frag_content):
        try:
            ctx['dest_stream'].write(frag_content)
            ctx['dest_stream'].flush()
        finally:
            if self.__do_ytdl_file(ctx):
                self._write_ytdl_file(ctx)
            if not self.params.get('keep_fragments', False):
                os.remove(encodeFilename(ctx['fragment_filename_sanitized']))
            del ctx['fragment_filename_sanitized']

    def _prepare_frag_download(self, ctx):
        if 'live' not in ctx:
            ctx['live'] = False
        if not ctx['live']:
            total_frags_str = '%d' % ctx['total_frags']
            ad_frags = ctx.get('ad_frags', 0)
            if ad_frags:
                total_frags_str += ' (not including %d ad)' % ad_frags
        else:
            total_frags_str = 'unknown (live)'
        self.to_screen(
            '[%s] Total fragments: %s' % (self.FD_NAME, total_frags_str))
        self.report_destination(ctx['filename'])
        dl = HttpQuietDownloader(
            self.ydl,
            {
                'continuedl': True,
                'quiet': True,
                'noprogress': True,
                'ratelimit': self.params.get('ratelimit'),
                'retries': self.params.get('retries', 0),
                'nopart': self.params.get('nopart', False),
                'test': self.params.get('test', False),
            }
        )
        tmpfilename = self.temp_name(ctx['filename'])
        open_mode = 'wb'
        resume_len = 0

        # Establish possible resume length
        if os.path.isfile(encodeFilename(tmpfilename)):
            open_mode = 'ab'
            resume_len = os.path.getsize(encodeFilename(tmpfilename))

        # Should be initialized before ytdl file check
        ctx.update({
            'tmpfilename': tmpfilename,
            'fragment_index': 0,
        })

        if self.__do_ytdl_file(ctx):
            if os.path.isfile(encodeFilename(self.ytdl_filename(ctx['filename']))):
                self._read_ytdl_file(ctx)
                is_corrupt = ctx.get('ytdl_corrupt') is True
                is_inconsistent = ctx['fragment_index'] > 0 and resume_len == 0
                if is_corrupt or is_inconsistent:
                    message = (
                        '.ytdl file is corrupt' if is_corrupt else
                        'Inconsistent state of incomplete fragment download')
                    self.report_warning(
                        '%s. Restarting from the beginning ...' % message)
                    ctx['fragment_index'] = resume_len = 0
                    if 'ytdl_corrupt' in ctx:
                        del ctx['ytdl_corrupt']
                    self._write_ytdl_file(ctx)
            else:
                self._write_ytdl_file(ctx)
                assert ctx['fragment_index'] == 0

        dest_stream, tmpfilename = sanitize_open(tmpfilename, open_mode)

        ctx.update({
            'dl': dl,
            'dest_stream': dest_stream,
            'tmpfilename': tmpfilename,
            # Total complete fragments downloaded so far in bytes
            'complete_frags_downloaded_bytes': resume_len,
        })

    def _start_frag_download(self, ctx, info_dict):
        resume_len = ctx['complete_frags_downloaded_bytes']
        total_frags = ctx['total_frags']
        # This dict stores the download progress, it's updated by the progress
        # hook
        state = {
            'status': 'downloading',
            'downloaded_bytes': resume_len,
            'fragment_index': ctx['fragment_index'],
            'fragment_count': total_frags,
            'filename': ctx['filename'],
            'tmpfilename': ctx['tmpfilename'],
        }

        start = time.time()
        ctx.update({
            'started': start,
            # Amount of fragment's bytes downloaded by the time of the previous
            # frag progress hook invocation
            'prev_frag_downloaded_bytes': 0,
        })

        def frag_progress_hook(s):
            if s['status'] not in ('downloading', 'finished'):
                return

            time_now = time.time()
            state['elapsed'] = time_now - start
            frag_total_bytes = s.get('total_bytes') or 0
            s['fragment_info_dict'] = s.pop('info_dict', {})
            if not ctx['live']:
                estimated_size = (
                    (ctx['complete_frags_downloaded_bytes'] + frag_total_bytes)
                    / (state['fragment_index'] + 1) * total_frags)
                state['total_bytes_estimate'] = estimated_size

            if s['status'] == 'finished':
                state['fragment_index'] += 1
                ctx['fragment_index'] = state['fragment_index']
                state['downloaded_bytes'] += frag_total_bytes - ctx['prev_frag_downloaded_bytes']
                ctx['complete_frags_downloaded_bytes'] = state['downloaded_bytes']
                ctx['prev_frag_downloaded_bytes'] = 0
            else:
                frag_downloaded_bytes = s['downloaded_bytes']
                state['downloaded_bytes'] += frag_downloaded_bytes - ctx['prev_frag_downloaded_bytes']
                if not ctx['live']:
                    state['eta'] = self.calc_eta(
                        start, time_now, estimated_size - resume_len,
                        state['downloaded_bytes'] - resume_len)
                state['speed'] = s.get('speed') or ctx.get('speed')
                ctx['speed'] = state['speed']
                ctx['prev_frag_downloaded_bytes'] = frag_downloaded_bytes
            self._hook_progress(state, info_dict)

        ctx['dl'].add_progress_hook(frag_progress_hook)

        return start

    def _finish_frag_download(self, ctx, info_dict):
        ctx['dest_stream'].close()
        if self.__do_ytdl_file(ctx):
            ytdl_filename = encodeFilename(self.ytdl_filename(ctx['filename']))
            if os.path.isfile(ytdl_filename):
                os.remove(ytdl_filename)
        elapsed = time.time() - ctx['started']

        if ctx['tmpfilename'] == '-':
            downloaded_bytes = ctx['complete_frags_downloaded_bytes']
        else:
            self.try_rename(ctx['tmpfilename'], ctx['filename'])
            if self.params.get('updatetime', True):
                filetime = ctx.get('fragment_filetime')
                if filetime:
                    try:
                        os.utime(ctx['filename'], (time.time(), filetime))
                    except Exception:
                        pass
            downloaded_bytes = os.path.getsize(encodeFilename(ctx['filename']))

        self._hook_progress({
            'downloaded_bytes': downloaded_bytes,
            'total_bytes': downloaded_bytes,
            'filename': ctx['filename'],
            'status': 'finished',
            'elapsed': elapsed,
        }, info_dict)

    def _prepare_external_frag_download(self, ctx):
        if 'live' not in ctx:
            ctx['live'] = False
        if not ctx['live']:
            total_frags_str = '%d' % ctx['total_frags']
            ad_frags = ctx.get('ad_frags', 0)
            if ad_frags:
                total_frags_str += ' (not including %d ad)' % ad_frags
        else:
            total_frags_str = 'unknown (live)'
        self.to_screen(
            '[%s] Total fragments: %s' % (self.FD_NAME, total_frags_str))

        tmpfilename = self.temp_name(ctx['filename'])

        # Should be initialized before ytdl file check
        ctx.update({
            'tmpfilename': tmpfilename,
            'fragment_index': 0,
        })

    def download_and_append_fragments(self, ctx, fragments, info_dict, pack_func=None):
        fragment_retries = self.params.get('fragment_retries', 0)
        is_fatal = (lambda idx: idx == 0) if self.params.get('skip_unavailable_fragments', True) else (lambda _: True)
        if not pack_func:
            pack_func = lambda frag_content, _: frag_content

        def download_fragment(fragment, ctx):
            frag_index = ctx['fragment_index'] = fragment['frag_index']
            headers = info_dict.get('http_headers', {})
            byte_range = fragment.get('byte_range')
            if byte_range:
                headers['Range'] = 'bytes=%d-%d' % (byte_range['start'], byte_range['end'] - 1)

            # Never skip the first fragment
            fatal = is_fatal(fragment.get('index') or (frag_index - 1))
            count, frag_content = 0, None
            while count <= fragment_retries:
                try:
                    success, frag_content = self._download_fragment(ctx, fragment['url'], info_dict, headers)
                    if not success:
                        return False, frag_index
                    break
                except compat_urllib_error.HTTPError as err:
                    # Unavailable (possibly temporary) fragments may be served.
                    # First we try to retry then either skip or abort.
                    # See https://github.com/ytdl-org/youtube-dl/issues/10165,
                    # https://github.com/ytdl-org/youtube-dl/issues/10448).
                    count += 1
                    if count <= fragment_retries:
                        self.report_retry_fragment(err, frag_index, count, fragment_retries)
                except DownloadError:
                    # Don't retry fragment if error occurred during HTTP downloading
                    # itself since it has own retry settings
                    if not fatal:
                        break
                    raise

            if count > fragment_retries:
                if not fatal:
                    return False, frag_index
                ctx['dest_stream'].close()
                self.report_error('Giving up after %s fragment retries' % fragment_retries)
                return False, frag_index
            return frag_content, frag_index

        def decrypt_fragment(fragment, frag_content):
            decrypt_info = fragment.get('decrypt_info')
            if not decrypt_info or decrypt_info['METHOD'] != 'AES-128':
                return frag_content
            iv = decrypt_info.get('IV') or compat_struct_pack('>8xq', fragment['media_sequence'])
            decrypt_info['KEY'] = decrypt_info.get('KEY') or self.ydl.urlopen(
                self._prepare_url(info_dict, info_dict.get('_decryption_key_url') or decrypt_info['URI'])).read()
            # Don't decrypt the content in tests since the data is explicitly truncated and it's not to a valid block
            # size (see https://github.com/ytdl-org/youtube-dl/pull/27660). Tests only care that the correct data downloaded,
            # not what it decrypts to.
            if self.params.get('test', False):
                return frag_content
            return AES.new(decrypt_info['KEY'], AES.MODE_CBC, iv).decrypt(frag_content)

        def append_fragment(frag_content, frag_index, ctx):
            if not frag_content:
                if not is_fatal(frag_index - 1):
                    self.report_skip_fragment(frag_index)
                    return True
                else:
                    ctx['dest_stream'].close()
                    self.report_error(
                        'fragment %s not found, unable to continue' % frag_index)
                    return False
            self._append_fragment(ctx, pack_func(frag_content, frag_index))
            return True

        max_workers = self.params.get('concurrent_fragment_downloads', 1)
        if can_threaded_download and max_workers > 1:

            def _download_fragment(fragment):
                ctx_copy = ctx.copy()
                frag_content, frag_index = download_fragment(fragment, ctx_copy)
                return fragment, frag_content, frag_index, ctx_copy.get('fragment_filename_sanitized')

            self.report_warning('The download speed shown is only of one thread. This is a known issue and patches are welcome')
            with concurrent.futures.ThreadPoolExecutor(max_workers) as pool:
                for fragment, frag_content, frag_index, frag_filename in pool.map(_download_fragment, fragments):
                    ctx['fragment_filename_sanitized'] = frag_filename
                    ctx['fragment_index'] = frag_index
                    result = append_fragment(decrypt_fragment(fragment, frag_content), frag_index, ctx)
                    if not result:
                        return False
        else:
            for fragment in fragments:
                frag_content, frag_index = download_fragment(fragment, ctx)
                result = append_fragment(decrypt_fragment(fragment, frag_content), frag_index, ctx)
                if not result:
                    return False

        self._finish_frag_download(ctx, info_dict)
        return True
