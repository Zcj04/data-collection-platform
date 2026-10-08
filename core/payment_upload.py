"""货款 XLSX 解析前的资源边界。"""
import io
import zipfile

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_ROWS = 10000


def read_upload(file):
    content = file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("文件超过 10 MB，请缩小后上传")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
                raise ValueError("工作簿解压规模过大，请只保留货款明细后上传")
            if any(e.flag_bits & 1 for e in entries):
                raise ValueError("不支持加密工作簿")
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效的 XLSX 工作簿") from exc
    return content
