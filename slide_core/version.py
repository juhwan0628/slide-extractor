"""Single release identity for runtime and native packaging."""
VERSION = "0.5.0b2"
BUNDLE_VERSION = "0.5.0"

if __name__ == "__main__":
    import sys
    print(BUNDLE_VERSION if "--bundle" in sys.argv else VERSION)
