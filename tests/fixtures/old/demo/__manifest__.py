{
    'name': 'Demo',
    'assets': {
        'web.assets_backend': [
            'demo/static/src/**/*',
            ('remove', 'demo/static/src/js/legacy.js'),
        ],
        'web.assets_frontend': ['demo/static/src/js/front.js'],
    },
}
