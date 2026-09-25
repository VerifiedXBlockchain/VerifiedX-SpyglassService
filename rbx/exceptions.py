class RBXException(Exception):
    pass


class UnsafeAssetFileName(Exception):
    """An NFT asset name the CLI would refuse as a file name (VX-04)."""

    def __init__(self, file_name: str):
        self.file_name = file_name
        super().__init__(
            f"Asset file name {file_name!r} is not a plain file name: it must not "
            "contain '/', '\\', ':', '..' or control characters, or end with a "
            "space or a dot."
        )
