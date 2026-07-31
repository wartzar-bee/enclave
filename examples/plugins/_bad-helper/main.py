#!/usr/bin/env python3
# RED fixture #2 entrypoint — deliberately CLEAN. Nothing here trips the vetting scan; the malicious
# behaviour lives in helper.py, which this file imports at load time. Exists only so the test suite
# can prove the validator scans the whole plugin dir, not just the declared entrypoint.
import helper


def main():
    helper.report()


if __name__ == "__main__":
    main()
