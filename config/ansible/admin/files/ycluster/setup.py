#!/usr/bin/env python3

from setuptools import setup, find_packages
import os

# Read version from __init__.py
def get_version():
    version_file = os.path.join(os.path.dirname(__file__), 'ycluster', '__init__.py')
    with open(version_file) as f:
        for line in f:
            if line.startswith('__version__'):
                return line.split('=')[1].strip().strip('"\'')
    return '0.1.0'

# Read requirements
def get_requirements():
    requirements_file = os.path.join(os.path.dirname(__file__), 'requirements.txt')
    if os.path.exists(requirements_file):
        with open(requirements_file) as f:
            return [line.strip() for line in f if line.strip() and not line.startswith('#')]
    return []

setup(
    name="ycluster",
    version=get_version(),
    description="YCluster Infrastructure Management Tools",
    long_description="Self-bootstrapping infrastructure platform management tools",
    author="YCluster Team",
    packages=find_packages(),
    install_requires=[
        "etcd3>=0.12.0",
        "flask>=2.0.0",
        "requests>=2.25.0",
        "dnspython>=2.1.0",
        "cryptography>=3.4.0",
        "jinja2>=3.0.0",
        "scapy>=2.4.0",
        "ntplib>=0.3.0",
        "netifaces>=0.11.0",
        "pyyaml>=5.4.0",
    ],
    entry_points={
        "console_scripts": [
            "ycluster=ycluster.cli.main:main",
            "yc-dhcp-server=ycluster.services.dhcp_server:main",
        ],
    },
    python_requires=">=3.8",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: System :: Systems Administration",
        "Topic :: System :: Clustering",
    ],
)
