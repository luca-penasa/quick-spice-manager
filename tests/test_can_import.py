def test_import_package():
    """Verify we can import the main package"""
    import quick_spice_manager

def test_has_version():
    """Check that the package has an accesible __version__"""
    import quick_spice_manager
    version = quick_spice_manager.__version__