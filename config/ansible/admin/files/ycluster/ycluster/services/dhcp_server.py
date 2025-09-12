"""
DHCP Server service wrapper
"""

from ..utils.dhcp_server import main as dhcp_main


def main():
    """Main entry point for DHCP server service"""
    # Import and run the original DHCP server
    dhcp_main()


if __name__ == '__main__':
    main()
