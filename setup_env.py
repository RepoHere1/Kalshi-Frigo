#!/usr/bin/env python3
"""
Kalshi AI Trading Bot - Setup and Environment Checker

This script helps users set up the bot correctly and diagnose common issues.
"""

import sys
import os
import subprocess
import platform
from pathlib import Path


def print_header(title):
    """Print a formatted header."""
    print(f"\n{'=' * 60}")
    print(f"🚀 {title}")
    print(f"{'=' * 60}")


def print_step(step, description):
    """Print a formatted step."""
    print(f"\n{step}. {description}")
    print("-" * 40)


def check_python_version():
    """Check if Python version is compatible."""
    version = sys.version_info
    print(f"Python version: {version.major}.{version.minor}.{version.micro}")
    
    if version.major == 3 and version.minor >= 12:
        if version.minor == 14:
            print("⚠️  Python 3.14 detected. You may encounter PyO3 issues.")
            print("   Consider using Python 3.12 or 3.13 for best compatibility.")
            return "warning"
        else:
            print("✅ Python version is compatible.")
            return "ok"
    else:
        print("❌ Python 3.12+ required. Please upgrade your Python version.")
        return "error"


def check_virtual_environment():
    """Check if running in a virtual environment."""
    in_venv = (
        sys.prefix != sys.base_prefix or 
        hasattr(sys, 'real_prefix') or
        os.environ.get('VIRTUAL_ENV') is not None
    )
    
    if in_venv:
        print("✅ Running in virtual environment.")
        if os.environ.get('VIRTUAL_ENV'):
            print(f"   Virtual env path: {os.environ['VIRTUAL_ENV']}")
        return True
    else:
        print("⚠️  Not running in a virtual environment.")
        print("   It's recommended to use a virtual environment.")
        return False


def check_dependencies():
    """Check if required dependencies are installed."""
    required_packages = [
        'pandas', 'numpy', 'aiosqlite', 'httpx', 'openai', 
        'anthropic', 'cryptography', 'pydantic'
    ]
    
    missing = []
    
    for package in required_packages:
        try:
            __import__(package)
            print(f"✅ {package}")
        except ImportError:
            print(f"❌ {package} (missing)")
            missing.append(package)
    
    return missing


def run_command(cmd, capture_output=True):
    """Run a shell command and return the result."""
    try:
        result = subprocess.run(
            cmd, 
            shell=True, 
            capture_output=capture_output, 
            text=True,
            timeout=30
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Command timed out"
    except Exception as e:
        return False, "", str(e)


def install_dependencies():
    """Install the project (and its dependencies) from pyproject.toml."""
    print("Installing dependencies...")

    # For Python 3.14, set the compatibility flag
    env = os.environ.copy()
    if sys.version_info.minor == 14:
        env['PYO3_USE_ABI3_FORWARD_COMPATIBILITY'] = '1'
        print("🔧 Setting PyO3 compatibility flag for Python 3.14")

    success, stdout, stderr = run_command(
        f"{sys.executable} -m pip install -e .",
        capture_output=True
    )
    
    if success:
        print("✅ Dependencies installed successfully.")
        return True
    else:
        print("❌ Failed to install dependencies.")
        print(f"Error: {stderr}")
        return False


def test_dashboard():
    """Test if the CLI can be imported."""
    print("Testing CLI import...")
    
    try:
        # Test import
        from cli import build_parser
        print("✅ CLI imports successfully.")
        
        return True
    except ImportError as e:
        print(f"❌ CLI import failed: {e}")
        return False
    except Exception as e:
        print(f"❌ Dashboard test failed: {e}")
        return False
    except Exception as e:
        print(f"❌ Dashboard test failed: {e}")
        return False


def create_venv_if_needed():
    """Create virtual environment if not in one."""
    if not check_virtual_environment():
        response = input("\nWould you like to create a virtual environment? (y/n): ")
        if response.lower().startswith('y'):
            print("Creating virtual environment...")
            success, _, _ = run_command(f"{sys.executable} -m venv .venv")
            if success:
                print("✅ Virtual environment created.")
                print("\nTo activate it:")
                if platform.system() == "Windows":
                    print("   .venv\\Scripts\\activate")
                else:
                    print("   source .venv/bin/activate")
                print("\nThen run this setup script again.")
                return False
            else:
                print("❌ Failed to create virtual environment.")
        return False
    return True


def main():
    """Main setup function."""
    print_header("Kalshi AI Trading Bot Setup")
    
    # Check if we're in the right directory
    if not Path("cli.py").exists():
        print("❌ cli.py not found.")
        print("   Make sure you're running this from the kalshi-ai-trading-bot directory.")
        return False
    
    print_step(1, "Checking Python Version")
    python_status = check_python_version()
    
    if python_status == "error":
        return False
    
    print_step(2, "Checking Virtual Environment")
    if not create_venv_if_needed():
        return False
    
    print_step(3, "Checking Dependencies")
    missing_deps = check_dependencies()
    
    if missing_deps:
        print(f"\n⚠️  Missing dependencies: {', '.join(missing_deps)}")
        response = input("Install missing dependencies? (y/n): ")
        if response.lower().startswith('y'):
            if not install_dependencies():
                return False
            
            # Check again
            missing_deps = check_dependencies()
            if missing_deps:
                print(f"❌ Still missing: {', '.join(missing_deps)}")
                return False
    
    print_step(4, "Testing Dashboard")
    if not test_dashboard():
        print("\n💡 Try these solutions:")
        print("   1. Make sure you activated your virtual environment")
        print("   2. Run: pip install -e .")
        print("   3. Check that you're in the project root directory")
        return False
    
    print_header("Setup Complete!")
    print("✅ Your environment is ready!")
    print("\n🚀 To run the bot:")
    print("   python cli.py run --paper")
    print("   python cli.py run --live")
    print("\n   For specific strategies:")
    print("   python cli.py run --btc-updown")
    print("   python cli.py run --btc-updown --series KXBTC1H --spot-product BTC-USD")
    print("\n   To view strategy status:")
    print("   python cli.py status")
    
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)