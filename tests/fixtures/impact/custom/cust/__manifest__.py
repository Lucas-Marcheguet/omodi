{
    'name': 'Cust',
    'depends': ['up', 'gone_mod', 'unknown_mod'],
    'assets': {
        'web.assets_backend': [
            'cust/static/src/**/*',
            ('before', 'up/static/src/js/gone_file.js', 'cust/static/src/js/x.js'),
            'up/static/src/js/lib.js',
        ],
        'up.bundle_gone': ['cust/static/src/js/y.js'],
    },
}
