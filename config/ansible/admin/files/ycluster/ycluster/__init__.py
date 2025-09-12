"""
YCluster Infrastructure Management Tools

A self-bootstrapping infrastructure platform that creates highly available 
clusters from bare metal servers.
"""

__version__ = "0.1.0"
__author__ = "YCluster Team"

# Make common utilities easily accessible
from .common import etcd_utils

__all__ = ['etcd_utils']
