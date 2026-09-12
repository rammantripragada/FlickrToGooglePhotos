from flickr_to_google_photos.integrity import sha256_file


def test_sha256_is_stable_and_distinguishes_content(tmp_path):
    first = tmp_path / "first.jpg"
    duplicate = tmp_path / "duplicate.jpg"
    different = tmp_path / "different.jpg"
    first.write_bytes(b"same photo bytes")
    duplicate.write_bytes(b"same photo bytes")
    different.write_bytes(b"different photo bytes")
    assert sha256_file(first) == sha256_file(duplicate)
    assert sha256_file(first) != sha256_file(different)
