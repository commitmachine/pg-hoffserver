from setuptools import setup

install_requirements = [
  'pgcli==1.2.0',
  'psycopg2==2.6.2',
  'simplejson>=2.1',
  'zerorpc>0.6.0'
]

setup(name='pg-hoffserver',
    version='0.2.0',
    description='json interface to pgcli using ZeroRPC',
    url='http://github.com/commitmachine/pg-hoffserver',
    author='commitmachine',
    author_email='commitmachine@asdasd.se',
    license='MIT',
    packages=['pghoffserver'],
    entry_points = {
        'console_scripts': ['pghoffserver=pghoffserver.pghoffserver:main']
    },
    install_requires=install_requirements,
    zip_safe=False)
