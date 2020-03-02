import re, sys, os
from gunicorn.app.wsgiapp import run

if __name__ == '__main__':
    sys.argv[0] = __file__
    sys.exit(run())
