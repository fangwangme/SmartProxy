#!/usr/bin/env python3
"""
SmartProxy Entry Point
Run with: python run.py [start|stop|restart|status]
"""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

if __name__ == "__main__":
    from main import main
    main()
