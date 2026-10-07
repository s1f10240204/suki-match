(function () {
    // 現在地取得ボタン: data-geo-fill="緯度inputのid,経度inputのid"
    document.querySelectorAll('[data-geo-fill]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var ids = btn.dataset.geoFill.split(',');
            if (!navigator.geolocation) { alert('この端末では位置情報を取得できません。'); return; }
            navigator.geolocation.getCurrentPosition(function (pos) {
                document.getElementById(ids[0]).value = pos.coords.latitude;
                document.getElementById(ids[1]).value = pos.coords.longitude;
            }, function () { alert('位置情報を取得できませんでした。'); });
        });
    });

    function toast(message, url) {
        var area = document.getElementById('toast-area');
        var el = document.createElement(url ? 'a' : 'div');
        el.className = 'toast';
        el.textContent = message;
        if (url && url.charAt(0) === '/') { el.href = url; }
        area.appendChild(el);
        setTimeout(function () { el.remove(); }, 10000);
    }

    // WebSocket: 通知の受信と現在地の送信(ログイン時のみ)
    if (document.body.dataset.wsEnabled !== '1') { return; }
    var scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    var retry = 1000;

    function connect() {
        var ws = new WebSocket(scheme + location.host + '/ws/notifications/');
        ws.onopen = function () {
            retry = 1000;
            if (navigator.geolocation) {
                navigator.geolocation.getCurrentPosition(function (pos) {
                    ws.send(JSON.stringify({ type: 'location', lat: pos.coords.latitude, lng: pos.coords.longitude }));
                }, function () {});
            }
        };
        ws.onmessage = function (e) {
            var data = JSON.parse(e.data);
            toast(data.message, data.url);
        };
        ws.onclose = function () {
            setTimeout(connect, retry);
            retry = Math.min(retry * 2, 30000);
        };
    }
    connect();
})();
