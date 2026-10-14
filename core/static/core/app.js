(function () {
    'use strict';

    // ---------- トースト ----------
    function toast(message, url, kind) {
        var area = document.getElementById('toast-area');
        if (!area) { return; }
        var el = document.createElement(url ? 'a' : 'div');
        el.className = 'toast' + (kind === 'warning' || kind === 'sanction' ? ' toast-warning' : '');
        el.textContent = message;
        if (url && url.charAt(0) === '/') { el.href = url; }
        area.appendChild(el);
        setTimeout(function () { el.remove(); }, 8000);
    }

    // ---------- カウントダウン ----------
    function formatRemaining(ms) {
        var total = Math.max(0, Math.floor(ms / 1000));
        var h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
        if (h > 0) { return '残り' + h + '時間' + m + '分'; }
        return '残り' + m + ':' + (s < 10 ? '0' : '') + s;
    }

    var countdowns = [];
    document.querySelectorAll('[data-countdown]').forEach(function (el) {
        var deadline = Date.parse(el.dataset.countdown);
        if (!isNaN(deadline)) { countdowns.push({ el: el, deadline: deadline, kind: 'listing' }); }
    });
    document.querySelectorAll('[data-countdown-seconds]').forEach(function (el) {
        countdowns.push({ el: el, deadline: Date.now() + Number(el.dataset.countdownSeconds) * 1000, kind: 'grace' });
    });

    function tick() {
        var now = Date.now();
        countdowns.forEach(function (c) {
            var left = c.deadline - now;
            if (c.kind === 'grace') {
                c.el.textContent = Math.max(0, Math.ceil(left / 1000)) + '秒';
                if (left <= 0 && c.el.parentNode) {
                    c.el.parentNode.className = 'notice';
                    c.el.parentNode.textContent = 'ペナルティなしでキャンセルできる時間は終了しました。';
                }
                return;
            }
            if (left <= 0) {
                c.el.textContent = '締切';
                c.el.classList.add('is-over');
                return;
            }
            c.el.textContent = formatRemaining(left);
            c.el.classList.toggle('is-urgent', left < 10 * 60 * 1000);
        });
    }
    if (countdowns.length) { tick(); setInterval(tick, 1000); }

    // ---------- 位置の指定(現在地 + 地図) ----------
    var GEO_ERRORS = {
        1: '位置情報の利用が許可されていません。ブラウザの設定で許可するか、地図をタップして指定してください。',
        2: '現在地を取得できませんでした。電波の良い場所で再試行するか、地図をタップして指定してください。',
        3: '現在地の取得がタイムアウトしました。もう一度お試しください。'
    };
    var DEFAULT_CENTER = [35.6812, 139.7671];

    function setupLocationPicker(root) {
        var latInput = document.getElementById(root.dataset.latInput);
        var lngInput = document.getElementById(root.dataset.lngInput);
        var status = root.querySelector('[data-geo-status]');
        var mapEl = root.querySelector('[data-map]');
        var map = null, marker = null;

        function setStatus(text, cls) {
            status.textContent = text;
            status.className = 'geo-status' + (cls ? ' ' + cls : '');
        }
        function setPoint(lat, lng, label) {
            latInput.value = lat.toFixed(6);
            lngInput.value = lng.toFixed(6);
            setStatus(label, 'is-ok');
            if (map) {
                if (marker) { marker.setLatLng([lat, lng]); } else { marker = window.L.marker([lat, lng]).addTo(map); }
                map.setView([lat, lng], Math.max(map.getZoom(), 16));
            }
        }

        root.querySelector('[data-geo-button]').addEventListener('click', function () {
            if (!window.isSecureContext) {
                setStatus('位置情報はHTTPS(または localhost)でのみ利用できます。地図をタップして指定してください。', 'is-error');
                return;
            }
            if (!navigator.geolocation) {
                setStatus('この端末・ブラウザでは位置情報を利用できません。地図をタップして指定してください。', 'is-error');
                return;
            }
            setStatus('現在地を取得しています…');
            navigator.geolocation.getCurrentPosition(function (pos) {
                setPoint(pos.coords.latitude, pos.coords.longitude, '現在地を設定しました。');
            }, function (err) {
                setStatus(GEO_ERRORS[err.code] || '現在地を取得できませんでした。', 'is-error');
            }, { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 });
        });

        function initMap() {
            if (!window.L || !mapEl) { return; }  // 地図ライブラリが読めない環境では現在地ボタンだけ使う
            var hasValue = latInput.value && lngInput.value;
            var center = hasValue ? [Number(latInput.value), Number(lngInput.value)] : DEFAULT_CENTER;
            mapEl.textContent = '';
            mapEl.style.display = 'block';
            map = window.L.map(mapEl).setView(center, hasValue ? 16 : 13);
            window.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
            }).addTo(map);
            if (hasValue) { marker = window.L.marker(center).addTo(map); }
            map.on('click', function (e) { setPoint(e.latlng.lat, e.latlng.lng, '地図で場所を指定しました。'); });
            // <details> の中など、表示されてからサイズを再計算する
            var details = root.closest('details');
            if (details) { details.addEventListener('toggle', function () { map.invalidateSize(); }); }
        }
        if (document.readyState === 'complete') { initMap(); } else { window.addEventListener('load', initMap); }
    }
    document.querySelectorAll('[data-location-picker]').forEach(setupLocationPicker);

    // ---------- スキルのタグ入力(候補から選択 + 新規追加) ----------
    function setupSkillPicker(original) {
        var url = original.dataset.skillPicker;
        var names = original.value.split(/[,、，]/).map(function (s) { return s.trim(); }).filter(Boolean);
        var wrap = document.createElement('div');
        wrap.className = 'skill-picker';
        var box = document.createElement('div');
        box.className = 'skill-picker-box';
        var input = document.createElement('input');
        input.type = 'text';
        input.placeholder = '入力して候補から選ぶ / Enterで追加';
        input.setAttribute('aria-label', original.getAttribute('aria-label') || 'スキル');
        var list = document.createElement('ul');
        list.className = 'skill-suggest';
        list.hidden = true;
        var quick = document.createElement('div');
        quick.className = 'skill-quick';

        original.type = 'hidden';
        original.parentNode.insertBefore(wrap, original);
        wrap.appendChild(box);
        wrap.appendChild(list);
        wrap.appendChild(quick);
        wrap.appendChild(original);

        var suggestions = [], active = -1, timer = null;

        function key(s) { return s.normalize('NFKC').toLowerCase().replace(/\s/g, ''); }
        function has(name) { return names.some(function (n) { return key(n) === key(name); }); }
        function sync() { original.value = names.join(', '); }
        function render() {
            box.querySelectorAll('.skill-chip').forEach(function (c) { c.remove(); });
            names.forEach(function (name, i) {
                var chip = document.createElement('span');
                chip.className = 'skill-chip';
                chip.textContent = name;
                var x = document.createElement('button');
                x.type = 'button';
                x.textContent = '×';
                x.setAttribute('aria-label', name + 'を削除');
                x.addEventListener('click', function () { names.splice(i, 1); sync(); render(); });
                chip.appendChild(x);
                box.insertBefore(chip, input);
            });
            quick.querySelectorAll('button').forEach(function (b) { b.hidden = has(b.dataset.name); });
        }
        function add(name) {
            name = name.trim().replace(/,/g, '').slice(0, 30);
            if (name && !has(name)) { names.push(name); sync(); render(); }
            input.value = '';
            hideList();
        }
        function hideList() { list.hidden = true; active = -1; }
        function showList() {
            list.textContent = '';
            var q = input.value.trim();
            var items = suggestions.filter(function (s) { return !has(s.name); });
            if (q && !items.some(function (s) { return key(s.name) === key(q); })) {
                items.push({ name: q, isNew: true });
            }
            items.forEach(function (s, i) {
                var li = document.createElement('li');
                li.textContent = s.isNew ? '「' + s.name + '」を新しく追加' : s.name;
                if (s.official) {
                    var tag = document.createElement('span');
                    tag.className = 'tag-official';
                    tag.textContent = '定番';
                    li.appendChild(tag);
                }
                li.classList.toggle('is-active', i === active);
                li.addEventListener('mousedown', function (e) { e.preventDefault(); add(s.name); });
                list.appendChild(li);
            });
            list.hidden = items.length === 0;
            list._items = items;
        }
        function fetchSuggestions(q, cb) {
            fetch(url + '?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
                .then(function (r) { return r.ok ? r.json() : { results: [] }; })
                .then(function (data) { cb(data.results || []); })
                .catch(function () { cb([]); });
        }

        input.addEventListener('input', function () {
            clearTimeout(timer);
            var q = input.value.trim();
            if (!q) { hideList(); return; }
            timer = setTimeout(function () {
                fetchSuggestions(q, function (results) { suggestions = results; active = 0; showList(); });
            }, 150);
        });
        input.addEventListener('keydown', function (e) {
            if (e.isComposing || e.keyCode === 229) { return; }  // 日本語変換の確定Enterでは追加しない
            var items = list._items || [];
            if (e.key === 'Enter' || e.key === ',' || e.key === '、') {
                e.preventDefault();
                if (!list.hidden && items[active]) { add(items[active].name); } else { add(input.value); }
            } else if (e.key === 'ArrowDown' && items.length) {
                e.preventDefault(); active = (active + 1) % items.length; showList();
            } else if (e.key === 'ArrowUp' && items.length) {
                e.preventDefault(); active = (active - 1 + items.length) % items.length; showList();
            } else if (e.key === 'Backspace' && !input.value && names.length) {
                names.pop(); sync(); render();
            } else if (e.key === 'Escape') {
                hideList();
            }
        });
        input.addEventListener('blur', function () { setTimeout(hideList, 100); });
        box.appendChild(input);
        box.addEventListener('click', function () { input.focus(); });

        // 定番スキルをワンタップで追加できるようにする
        fetchSuggestions('', function (results) {
            results.filter(function (s) { return s.official; }).forEach(function (s) {
                var b = document.createElement('button');
                b.type = 'button';
                b.dataset.name = s.name;
                b.textContent = '＋ ' + s.name;
                b.addEventListener('click', function () { add(s.name); });
                quick.appendChild(b);
            });
            render();
        });
        render();
    }
    document.querySelectorAll('input[data-skill-picker]').forEach(setupSkillPicker);

    // ---------- WebSocket ----------
    var scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';

    function connect(path, handlers) {
        var retry = 1000, ws;
        function open() {
            ws = new WebSocket(scheme + location.host + path);
            ws.onopen = function () { retry = 1000; if (handlers.open) { handlers.open(ws); } };
            ws.onmessage = function (e) { handlers.message(JSON.parse(e.data)); };
            ws.onclose = function (e) {
                if (e.code === 1006 || e.code === 1001 || e.code === 1011) {
                    setTimeout(open, retry);
                    retry = Math.min(retry * 2, 30000);
                }
            };
        }
        open();
        return { send: function (data) { if (ws && ws.readyState === 1) { ws.send(JSON.stringify(data)); return true; } return false; } };
    }

    if (document.body.dataset.wsEnabled === '1') {
        var REFRESH_KINDS = ['matched', 'applied', 'completed', 'canceled', 'rejected', 'expired'];
        connect('/ws/notifications/', {
            open: function (ws) {
                // 位置情報がすでに許可されているときだけ現在地を送る(勝手に許可ダイアログを出さない)
                if (!navigator.geolocation || !navigator.permissions) { return; }
                navigator.permissions.query({ name: 'geolocation' }).then(function (p) {
                    if (p.state !== 'granted') { return; }
                    navigator.geolocation.getCurrentPosition(function (pos) {
                        ws.send(JSON.stringify({ type: 'location', lat: pos.coords.latitude, lng: pos.coords.longitude }));
                    }, function () {}, { maximumAge: 5 * 60 * 1000 });
                }).catch(function () {});
            },
            message: function (data) {
                if (data.kind === 'message' && document.getElementById('chat-log') && data.url === location.pathname) {
                    return;  // 開いているチャットの通知は出さない
                }
                toast(data.message, data.url, data.kind);
                var badge = document.getElementById('unread-count');
                if (badge) { badge.textContent = Number(badge.textContent) + 1; }
                // 今見ている依頼の状況が変わったら画面を更新する
                if (REFRESH_KINDS.indexOf(data.kind) !== -1 && data.url === location.pathname) {
                    setTimeout(function () { location.reload(); }, 1500);
                }
            }
        });
    }

    // ---------- チャット ----------
    var log = document.getElementById('chat-log');
    if (log) {
        var me = Number(log.dataset.userId);
        var form = document.getElementById('chat-form');
        var seen = {};
        var scrollDown = function () { window.scrollTo(0, document.body.scrollHeight); };

        var appendMessage = function (m) {
            if (seen[m.id]) { return; }
            seen[m.id] = true;
            var mine = m.sender_id === me;
            if (m.hidden && !mine) { return; }
            var row = document.createElement('div');
            row.className = 'bubble-row' + (mine ? ' mine' : '');
            var bubble = document.createElement('div');
            bubble.className = 'bubble' + (m.hidden ? ' bubble-hidden' : '');
            bubble.textContent = m.hidden ? 'このメッセージは相手に表示されていません' : m.body;
            var meta = document.createElement('span');
            meta.className = 'bubble-meta';
            meta.textContent = m.created_at;
            if (!mine) {
                var report = document.createElement('a');
                report.href = log.dataset.reportUrl + '?message=' + m.id;
                report.textContent = '通報';
                meta.appendChild(document.createTextNode(' · '));
                meta.appendChild(report);
            }
            row.appendChild(bubble);
            row.appendChild(meta);
            log.appendChild(row);
            scrollDown();
        };

        var chat = connect('/ws/chat/' + log.dataset.chatId + '/', {
            message: function (data) {
                if (data.error) { toast(data.error, '', 'warning'); return; }
                appendMessage(data.message);
            }
        });
        if (form) {
            var bodyInput = form.querySelector('input[name=body]');
            form.addEventListener('submit', function (e) {
                var body = bodyInput.value.trim();
                if (!body) { e.preventDefault(); return; }
                if (chat.send({ body: body })) {  // 接続できないときは通常のフォーム送信にフォールバック
                    e.preventDefault();
                    bodyInput.value = '';
                }
            });
        }
        scrollDown();
    }
})();
