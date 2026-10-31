(function () {
    'use strict';

    // ---------- トースト ----------
    function toast(message, url, kind) {
        var area = document.getElementById('toast-area');
        if (!area) { return; }
        var el = document.createElement(url ? 'a' : 'div');
        el.className = 'toast' + (kind === 'warning' || kind === 'sanction' ? ' toast-warning' : kind === 'info' ? ' toast-info' : '');
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

    // ---------- 地図(Google Maps / OpenStreetMap を同じ形で扱う) ----------
    var GEO_ERRORS = {
        1: '位置情報の利用が許可されていません。ブラウザの設定で許可するか、住所で検索してください。',
        2: '現在地を取得できませんでした。電波の良い場所で再試行するか、住所で検索してください。',
        3: '現在地の取得がタイムアウトしました。もう一度お試しください。'
    };
    var DEFAULT_CENTER = { lat: 35.6812, lng: 139.7671 };
    var MAPS = window.SUKI_MAPS || {};

    function whenMapsReady(cb) {
        if (MAPS.provider === 'google') {
            if (MAPS.ready) { cb(); } else { document.addEventListener('suki:maps-ready', cb, { once: true }); }
        } else if (document.readyState === 'complete') {
            cb();
        } else {
            window.addEventListener('load', cb, { once: true });
        }
    }

    // 地図を作り、{ setMarker, onPick, refresh } を返す。ライブラリが読めなければ null
    function createMap(el, center, zoom, draggable) {
        el.textContent = '';
        if (MAPS.provider === 'google' && window.google && google.maps) {
            var gmap = new google.maps.Map(el, {
                center: center, zoom: zoom, mapId: MAPS.mapId, disableDefaultUI: true, zoomControl: true,
                gestureHandling: 'greedy', clickableIcons: false
            });
            var gmarker = null, pickHandlers = [];
            var emit = function (pos) { pickHandlers.forEach(function (h) { h(pos.lat, pos.lng); }); };
            var placeMarker = function (pos) {
                if (!gmarker) {
                    gmarker = new google.maps.marker.AdvancedMarkerElement({ map: gmap, position: pos, gmpDraggable: draggable });
                    gmarker.addListener('dragend', function () {
                        var p = gmarker.position;
                        emit({ lat: typeof p.lat === 'function' ? p.lat() : p.lat, lng: typeof p.lng === 'function' ? p.lng() : p.lng });
                    });
                } else {
                    gmarker.position = pos;
                }
            };
            if (draggable) {
                gmap.addListener('click', function (e) {
                    var p = { lat: e.latLng.lat(), lng: e.latLng.lng() };
                    placeMarker(p);
                    emit(p);
                });
            }
            return {
                setMarker: function (pos, focus) {
                    placeMarker(pos);
                    if (focus) { gmap.setCenter(pos); gmap.setZoom(Math.max(gmap.getZoom(), 16)); }
                },
                onPick: function (h) { pickHandlers.push(h); },
                refresh: function () {}
            };
        }
        if (window.L) {
            var lmap = window.L.map(el, { zoomControl: true }).setView([center.lat, center.lng], zoom);
            // OpenStreetMap 標準タイル: 白基調で読みやすい、Google マップに近い見た目(APIキー不要)
            window.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
                maxZoom: 19,
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
            }).addTo(lmap);
            var lmarker = null, handlers = [];
            var place = function (pos) {
                if (!lmarker) {
                    lmarker = window.L.marker([pos.lat, pos.lng], { draggable: draggable }).addTo(lmap);
                    lmarker.on('dragend', function () {
                        var p = lmarker.getLatLng();
                        handlers.forEach(function (h) { h(p.lat, p.lng); });
                    });
                } else {
                    lmarker.setLatLng([pos.lat, pos.lng]);
                }
            };
            if (draggable) {
                lmap.on('click', function (e) {
                    place(e.latlng);
                    handlers.forEach(function (h) { h(e.latlng.lat, e.latlng.lng); });
                });
            }
            return {
                setMarker: function (pos, focus) {
                    place(pos);
                    if (focus) { lmap.setView([pos.lat, pos.lng], Math.max(lmap.getZoom(), 16)); }
                },
                onPick: function (h) { handlers.push(h); },
                refresh: function () { lmap.invalidateSize(); }
            };
        }
        el.classList.add('map-unavailable');
        el.textContent = '地図を読み込めませんでした。住所検索または現在地で指定してください。';
        return null;
    }

    // 住所・地名・駅名・郵便番号 → 候補リスト。サーバー側で国土地理院(住所)と
    // Nominatim(駅名・施設名)を併用し、郵便番号にも対応する。base があれば近い順。
    function searchPlaces(query, baseLat, baseLng) {
        if (MAPS.provider === 'google' && window.google && google.maps) {
            return new google.maps.Geocoder().geocode({ address: query, region: 'jp' }).then(function (res) {
                return res.results.slice(0, 8).map(function (r) {
                    return {
                        name: r.formatted_address.replace(/^日本、(〒\d{3}-\d{4} )?/, ''),
                        lat: r.geometry.location.lat(), lng: r.geometry.location.lng()
                    };
                });
            });
        }
        var url = '/api/place-search/?q=' + encodeURIComponent(query);
        if (baseLat !== null && baseLng !== null) { url += '&lat=' + baseLat + '&lng=' + baseLng; }
        return fetch(url)
            .then(function (r) { return r.json(); })
            .then(function (d) { return d.results || []; });
    }

    function setupLocationPicker(root) {
        var latInput = document.getElementById(root.dataset.latInput);
        var lngInput = document.getElementById(root.dataset.lngInput);
        var status = root.querySelector('[data-geo-status]');
        var mapEl = root.querySelector('[data-map]');
        var addressInput = root.querySelector('[data-address-input]');
        var map = null;

        function setStatus(text, cls) {
            status.textContent = text;
            status.className = 'geo-status' + (cls ? ' ' + cls : '');
        }
        function setPoint(lat, lng, label, focus, place) {
            var rl = root.querySelector('[data-place-results]');
            if (rl) { rl.hidden = true; rl.innerHTML = ''; }
            latInput.value = Number(lat).toFixed(6);
            lngInput.value = Number(lng).toFixed(6);
            setStatus(label, 'is-ok');
            root.dataset.placeLabel = place;
            if (map) { map.setMarker({ lat: Number(lat), lng: Number(lng) }, focus); }
            var err = document.querySelector('[data-location-error]');
            if (err) { err.remove(); }
        }

        var resultsList = root.querySelector('[data-place-results]');
        function hideResults() {
            if (resultsList) { resultsList.hidden = true; resultsList.innerHTML = ''; }
        }
        function showResults(cands) {
            if (!resultsList) { return; }
            resultsList.innerHTML = '';
            cands.forEach(function (c) {
                var li = document.createElement('li');
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.textContent = c.name;
                btn.addEventListener('click', function () {
                    hideResults();
                    setPoint(c.lat, c.lng, '「' + c.name + '」にピンを置きました。ずれていたらピンを動かしてください。', true, c.name);
                });
                li.appendChild(btn);
                resultsList.appendChild(li);
            });
            resultsList.hidden = false;
        }
        function search() {
            var q = addressInput.value.trim();
            if (!q) { addressInput.focus(); return; }
            setStatus('「' + q + '」を検索しています…');
            hideResults();
            var baseLat = parseFloat(latInput.value);
            var baseLng = parseFloat(lngInput.value);
            searchPlaces(q, isNaN(baseLat) ? null : baseLat, isNaN(baseLng) ? null : baseLng).then(function (cands) {
                if (!cands.length) {
                    setStatus('「' + q + '」が見つかりませんでした。市区町村から入力するか、地図をタップしてください。', 'is-error');
                } else if (cands.length === 1) {
                    setPoint(cands[0].lat, cands[0].lng, '「' + cands[0].name + '」にピンを置きました。ずれていたらピンを動かしてください。', true, cands[0].name);
                } else {
                    setStatus('候補が ' + cands.length + ' 件見つかりました。下から選んでください。');
                    showResults(cands);
                }
            }).catch(function () {
                setStatus('「' + q + '」が見つかりませんでした。市区町村から入力するか、地図をタップしてください。', 'is-error');
            });
        }
        root.querySelector('[data-address-button]').addEventListener('click', search);
        addressInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); search(); }
        });

        root.querySelector('[data-geo-button]').addEventListener('click', function () {
            if (!window.isSecureContext) {
                setStatus('位置情報はHTTPS(または localhost)でのみ利用できます。住所で検索してください。', 'is-error');
                return;
            }
            if (!navigator.geolocation) {
                setStatus('この端末・ブラウザでは位置情報を利用できません。住所で検索してください。', 'is-error');
                return;
            }
            setStatus('現在地を取得しています…');
            navigator.geolocation.getCurrentPosition(function (pos) {
                setPoint(pos.coords.latitude, pos.coords.longitude, '現在地にピンを置きました。', true, '現在地');
                // 上の住所入力欄にも現在地の地名を入れる(取得できなければ「現在地」のまま)
                if (addressInput) {
                    addressInput.value = '現在地';
                    fetch('/api/area-label/?lat=' + pos.coords.latitude + '&lng=' + pos.coords.longitude)
                        .then(function (r) { return r.json(); })
                        .then(function (d) {
                            if (d && d.label) {
                                addressInput.value = d.label;
                                setPoint(pos.coords.latitude, pos.coords.longitude,
                                         d.label + '(現在地)にピンを置きました。', false, d.label);
                            }
                        }).catch(function () {});
                }
            }, function (err) {
                setStatus(GEO_ERRORS[err.code] || '現在地を取得できませんでした。', 'is-error');
            }, { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 });
        });

        whenMapsReady(function () {
            var hasValue = latInput.value && lngInput.value;
            var center = hasValue ? { lat: Number(latInput.value), lng: Number(lngInput.value) } : DEFAULT_CENTER;
            map = createMap(mapEl, center, hasValue ? 16 : 13, true);
            if (!map) { return; }
            if (hasValue) { map.setMarker(center, false); }
            map.onPick(function (lat, lng) { setPoint(lat, lng, '地図で場所を指定しました。', false, '地図で指定した場所'); });
            // 非表示の領域(<details> やステップ入力)から表示されたときに地図のサイズを直す
            root.addEventListener('suki:shown', function () { map.refresh(); });
            var details = root.closest('details');
            if (details) { details.addEventListener('toggle', function () { map.refresh(); }); }
        });
    }
    document.querySelectorAll('[data-location-picker]').forEach(setupLocationPicker);

    // 募集人数: 「5人以上」を選んだときだけ人数入力欄を表示する
    var headcountCustom = document.querySelector('[data-headcount-custom]');
    if (headcountCustom) {
        var syncHeadcount = function () {
            var checked = document.querySelector('input[name="headcount_choice"]:checked');
            headcountCustom.hidden = !(checked && checked.value === 'custom');
        };
        document.querySelectorAll('input[name="headcount_choice"]').forEach(function (radio) {
            radio.addEventListener('change', syncHeadcount);
        });
        syncHeadcount();
    }

    // 閲覧用の地図(マッチング成立後の正確な場所)
    document.querySelectorAll('[data-map-view]').forEach(function (el) {
        whenMapsReady(function () {
            var pos = { lat: Number(el.dataset.lat), lng: Number(el.dataset.lng) };
            var map = createMap(el, pos, 16, false);
            if (map) { map.setMarker(pos, false); }
        });
    });

    // ---------- 依頼フォームのステップ入力 ----------
    var stepper = document.querySelector('[data-stepper]');
    if (stepper) {
        var steps = Array.prototype.slice.call(stepper.querySelectorAll('[data-step]'));
        var dots = stepper.querySelectorAll('[data-step-dot]');
        var back = stepper.querySelector('[data-step-back]');
        var next = stepper.querySelector('[data-step-next]');
        var submit = stepper.querySelector('[data-step-submit]');
        var current = 0;

        // サーバー側のエラーがあれば、そのステップから始める
        steps.some(function (s, i) {
            if (s.querySelector('.has-error, .error')) { current = i; return true; }
            return false;
        });

        var laterBox = stepper.querySelector('[data-when-later]');
        var nowBox = stepper.querySelector('[data-when-now]');
        var isLater = function () { return stepper.querySelector('[data-when]:checked').value === 'later'; };
        var syncWhen = function () {
            laterBox.hidden = !isLater();
            nowBox.hidden = isLater();
            if (!isLater()) { laterBox.querySelector('input').value = ''; }
        };
        stepper.querySelectorAll('[data-when]').forEach(function (r) { r.addEventListener('change', syncWhen); });
        syncWhen();

        var val = function (sel) { var el = stepper.querySelector(sel); return el ? el.value.trim() : ''; };
        var fillSummary = function () {
            stepper.querySelector('[data-summary-title]').textContent = val('[name=title]') || '-';
            var dur = stepper.querySelector('[name=duration_minutes]:checked');
            var when = isLater() && val('[name=scheduled_for]') ? val('[name=scheduled_for]').replace('T', ' ') + '〜' : '今すぐ';
            stepper.querySelector('[data-summary-when]').textContent = when + (dur ? '(' + dur.parentNode.textContent.trim() + ')' : '');
            var picker = stepper.querySelector('[data-location-picker]');
            var place = [picker && picker.dataset.placeLabel, val('[name=address_detail]')].filter(Boolean).join(' ')
                || (val('[name=latitude]') ? '登録済みの現在地' : '-');
            stepper.querySelector('[data-summary-place]').textContent = place;
            var balance = parseInt(stepper.dataset.pointsBalance || '0', 10);
            var cost = dur ? parseInt(dur.value, 10) : 0;
            var summaryPoints = stepper.querySelector('[data-summary-points]');
            if (summaryPoints) {
                summaryPoints.textContent = cost
                    ? cost + 'pt(残高 ' + balance + 'pt → 投稿後 ' + (balance - cost) + 'pt)'
                    : '-';
            }
            var costBox = stepper.querySelector('[data-points-cost]');
            if (costBox) {
                if (!cost) {
                    costBox.textContent = '';
                } else if (balance < cost) {
                    costBox.innerHTML = 'この依頼では <strong>' + cost + 'pt</strong> を使います。'
                        + '<span class="points-short">ポイントが不足しています(保有 ' + balance + 'pt)。お手伝いをするとポイントが貯まります。</span>';
                } else {
                    costBox.innerHTML = 'この依頼では <strong>' + cost + 'pt</strong> を使います(現在の残高 ' + balance + 'pt)';
                }
            }
        };
        stepper.querySelectorAll('[name=duration_minutes]').forEach(function (r) {
            r.addEventListener('change', fillSummary);
        });

        var validate = function (step) {
            var ok = true;
            step.querySelectorAll('input[required], textarea[required], select[required]').forEach(function (el) {
                if (!el.closest('[hidden]') && !el.value.trim()) {
                    ok = false;
                    if (el.closest('.field')) { el.closest('.field').classList.add('has-error'); }
                }
            });
            if (isLater() && step.contains(laterBox) && !val('[name=scheduled_for]')) {
                ok = false;
                laterBox.querySelector('.field').classList.add('has-error');
            }
            if (step.querySelector('[data-location-picker]') && !val('[name=latitude]')) {
                ok = false;
                toast('場所を指定してください(住所で検索、現在地、または地図をタップ)。', '', 'warning');
            }
            if (!ok) {
                var first = step.querySelector('.has-error input, .has-error textarea');
                if (first) { first.focus(); }
            }
            return ok;
        };

        var show = function (i) {
            current = i;
            steps.forEach(function (s, n) { s.classList.toggle('is-current', n === i); });
            dots.forEach(function (d, n) { d.classList.toggle('is-current', n === i); d.classList.toggle('is-done', n < i); });
            back.hidden = i === 0;
            next.hidden = i === steps.length - 1;
            submit.hidden = i !== steps.length - 1;
            if (i === steps.length - 1) { fillSummary(); }
            var picker = steps[i].querySelector('[data-location-picker]');
            if (picker) { setTimeout(function () { picker.dispatchEvent(new Event('suki:shown')); }, 30); }
            window.scrollTo({ top: 0, behavior: 'smooth' });
        };
        next.addEventListener('click', function () { if (validate(steps[current])) { show(current + 1); } });
        back.addEventListener('click', function () { show(current - 1); });
        stepper.addEventListener('input', function (e) {
            var field = e.target.closest('.field');
            if (field) { field.classList.remove('has-error'); }
        });
        // 途中のステップでEnterを押しても送信されないようにする
        stepper.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && e.target.tagName === 'INPUT' && current < steps.length - 1) { e.preventDefault(); }
        });
        // ---------- 下書きの自動保存(この端末の localStorage に保存) ----------
        var DRAFT_KEY = 'sukiMatchRequestDraft:v1';
        var draftSubmitted = false;
        var draftFields = ['title', 'description', 'scheduled_for', 'address_detail',
                           'latitude', 'longitude', 'skill_names', 'headcount_custom'];
        var draftRadios = ['duration_minutes', 'mode', 'headcount_choice', 'chat_retention_days'];
        var saveDraft = function () {
            if (draftSubmitted) { return; }
            try {
                var data = {};
                draftFields.forEach(function (name) {
                    var el = stepper.querySelector('[name=' + name + ']');
                    if (el) { data[name] = el.value; }
                });
                draftRadios.forEach(function (name) {
                    var checked = stepper.querySelector('[name=' + name + ']:checked');
                    if (checked) { data[name] = checked.value; }
                });
                var when = stepper.querySelector('[data-when]:checked');
                if (when) { data.when = when.value; }
                data.preset_tags = Array.prototype.map.call(
                    stepper.querySelectorAll('[name=preset_tags]:checked'), function (c) { return c.value; });
                var picker = stepper.querySelector('[data-location-picker]');
                if (picker) { data.place_label = picker.dataset.placeLabel || ''; }
                localStorage.setItem(DRAFT_KEY, JSON.stringify(data));
            } catch (e) { /* 保存できなくてもフォームは使える */ }
        };
        var draftTimer = null;
        var queueSaveDraft = function () {
            clearTimeout(draftTimer);
            draftTimer = setTimeout(saveDraft, 600);
        };
        stepper.addEventListener('input', queueSaveDraft);
        stepper.addEventListener('change', queueSaveDraft);
        window.addEventListener('pagehide', saveDraft);
        stepper.addEventListener('submit', function () {
            draftSubmitted = true;
            try { localStorage.removeItem(DRAFT_KEY); } catch (e) {}
        });

        var draftNote = stepper.querySelector('[data-draft-note]');
        var hasServerValues = Boolean(val('[name=title]')) || Boolean(stepper.querySelector('.error, .has-error'));
        var isRepost = /[?&]from=/.test(window.location.search);
        if (!hasServerValues && !isRepost) {
            try {
                var draft = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null');
                if (draft && (draft.title || draft.description || draft.latitude)) {
                    draftFields.forEach(function (name) {
                        var el = stepper.querySelector('[name=' + name + ']');
                        if (el && draft[name]) { el.value = draft[name]; }
                    });
                    draftRadios.forEach(function (name) {
                        if (!draft[name]) { return; }
                        var radio = stepper.querySelector('[name=' + name + '][value="' + draft[name] + '"]');
                        if (radio) { radio.checked = true; }
                    });
                    if (draft.when) {
                        var whenRadio = stepper.querySelector('[data-when][value="' + draft.when + '"]');
                        if (whenRadio) { whenRadio.checked = true; syncWhen(); }
                    }
                    if (draft.preset_tags) {
                        stepper.querySelectorAll('[name=preset_tags]').forEach(function (c) {
                            c.checked = draft.preset_tags.indexOf(c.value) !== -1;
                        });
                    }
                    var draftPicker = stepper.querySelector('[data-location-picker]');
                    if (draftPicker && draft.place_label) { draftPicker.dataset.placeLabel = draft.place_label; }
                    if (draftNote) { draftNote.hidden = false; }
                    fillSummary();
                }
            } catch (e) { /* 下書きが壊れていても無視する */ }
        }
        if (draftNote) {
            var draftClear = draftNote.querySelector('[data-draft-clear]');
            if (draftClear) {
                draftClear.addEventListener('click', function () {
                    try { localStorage.removeItem(DRAFT_KEY); } catch (e) {}
                    draftNote.hidden = true;
                    toast('保存していた下書きを消しました。今の入力はそのまま残っています。', '', 'info');
                });
            }
        }

        stepper.classList.add('is-enhanced');
        show(current);
    }

    // ---------- 確認コード: 数字だけ受け付け、6桁で送信 / 再送ボタンのタイマー ----------
    document.querySelectorAll('[data-code-input]').forEach(function (input) {
        input.addEventListener('input', function () {
            input.value = input.value.normalize('NFKC').replace(/\D/g, '').slice(0, 6);
            if (input.value.length === 6 && input.form.requestSubmit) { input.form.requestSubmit(); }
        });
    });
    document.querySelectorAll('[data-resend-seconds]').forEach(function (button) {
        var label = button.textContent;
        var until = Date.now() + Number(button.dataset.resendSeconds) * 1000;
        var update = function () {
            var left = Math.ceil((until - Date.now()) / 1000);
            if (left > 0) {
                button.disabled = true;
                button.textContent = label + '(あと' + left + '秒)';
                setTimeout(update, 1000);
            } else {
                button.disabled = false;
                button.textContent = label;
            }
        };
        update();
    });

    // ---------- 押すとメッセージだけ出すボタン(例: マッチング前のメッセージタブ) ----------
    document.querySelectorAll('[data-toast]').forEach(function (el) {
        el.addEventListener('click', function (e) { e.preventDefault(); toast(el.dataset.toast, '', 'info'); });
    });

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
                if (s.official || s.similar) {
                    var tag = document.createElement('span');
                    tag.className = s.similar ? 'tag-similar' : 'tag-official';
                    tag.textContent = s.similar ? '似ているタグ' : '定番';
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

    // ---------- 作業タイマーの経過表示 ----------
    document.querySelectorAll('[data-elapsed-since]').forEach(function (el) {
        var started = Date.parse(el.getAttribute('data-elapsed-since'));
        if (isNaN(started)) { return; }
        var render = function () {
            var min = Math.max(0, Math.floor((Date.now() - started) / 60000));
            el.textContent = '（開始から約' + min + '分経過）';
        };
        render();
        setInterval(render, 30000);
    });

    // ---------- チャット ----------
    var log = document.getElementById('chat-log');
    if (log) {
        var me = Number(log.dataset.userId);
        var form = document.getElementById('chat-form');
        var seen = {};
        var scrollDown = function () { window.scrollTo(0, document.body.scrollHeight); };

        var REACTION_EMOJIS = ['❤️', '👍', '😊', '😮', '🙏', '🎉'];
        var renderReactions = function (el, reactions) {
            el.innerHTML = '';
            (reactions || []).forEach(function (r) {
                var chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'reaction-chip' + (r.user_ids.indexOf(me) !== -1 ? ' mine' : '');
                chip.dataset.emoji = r.emoji;
                chip.textContent = r.emoji;
                var count = document.createElement('span');
                count.textContent = r.count;
                chip.appendChild(count);
                el.appendChild(chip);
            });
        };
        var updateReactions = function (messageId, reactions) {
            var el = log.querySelector('[data-reactions-for="' + messageId + '"]');
            if (el) { renderReactions(el, reactions); }
        };
        var reactionBar = document.createElement('div');
        reactionBar.className = 'reaction-bar';
        reactionBar.hidden = true;
        var emojiRow = document.createElement('div');
        emojiRow.className = 'reaction-emojis';
        REACTION_EMOJIS.forEach(function (emoji) {
            var b = document.createElement('button');
            b.type = 'button';
            b.textContent = emoji;
            b.setAttribute('aria-label', 'リアクション ' + emoji);
            b.addEventListener('click', function () {
                sendReaction(Number(reactionBar.dataset.messageId), emoji);
                reactionBar.hidden = true;
            });
            emojiRow.appendChild(b);
        });
        reactionBar.appendChild(emojiRow);
        var actionsRow = document.createElement('div');
        actionsRow.className = 'bar-actions';
        var replyAction = document.createElement('button');
        replyAction.type = 'button';
        replyAction.textContent = '↩ 返信';
        replyAction.addEventListener('click', function () {
            startReply(Number(reactionBar.dataset.messageId));
            reactionBar.hidden = true;
        });
        var copyAction = document.createElement('button');
        copyAction.type = 'button';
        copyAction.textContent = '📋 コピー';
        copyAction.addEventListener('click', function () {
            copyMessage(Number(reactionBar.dataset.messageId));
            reactionBar.hidden = true;
        });
        var hideAction = document.createElement('button');
        hideAction.type = 'button';
        hideAction.textContent = '🗑 自分の画面から消す';
        hideAction.addEventListener('click', function () {
            hideMessage(Number(reactionBar.dataset.messageId));
            reactionBar.hidden = true;
        });
        actionsRow.appendChild(replyAction);
        actionsRow.appendChild(copyAction);
        actionsRow.appendChild(hideAction);
        reactionBar.appendChild(actionsRow);
        document.body.appendChild(reactionBar);

        // ---------- 返信とコピー ----------
        var replyBar = document.getElementById('reply-bar');
        var replyToInput = document.getElementById('reply-to-input');
        var replyToId = null;
        var clearReply = function () {
            replyToId = null;
            if (replyToInput) { replyToInput.value = ''; }
            if (replyBar) { replyBar.hidden = true; }
        };
        var startReply = function (id) {
            var bubble = log.querySelector('[data-message-id="' + id + '"]');
            if (!bubble) { return; }
            var textEl = bubble.querySelector('.bubble-text');
            var excerpt = textEl && textEl.textContent.trim()
                ? textEl.textContent.trim().replace(/\s+/g, ' ').slice(0, 40)
                : (bubble.querySelector('.bubble-img') ? '📷 写真'
                   : (bubble.querySelector('.bubble-stamp-emoji')
                       ? bubble.querySelector('.bubble-stamp-emoji').textContent + ' スタンプ' : ''));
            replyToId = id;
            if (replyToInput) { replyToInput.value = id; }
            if (replyBar) {
                replyBar.querySelector('[data-reply-name]').textContent = bubble.dataset.senderName || '';
                replyBar.querySelector('[data-reply-text]').textContent = excerpt;
                replyBar.hidden = false;
            }
            var bi = form ? form.querySelector('input[name=body]') : null;
            if (bi) { bi.focus(); }
        };
        var replyCancel = document.getElementById('reply-cancel');
        if (replyCancel) { replyCancel.addEventListener('click', clearReply); }
        var fallbackCopy = function (text) {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); } catch (e) {}
            document.body.removeChild(ta);
        };
        var hideMessage = function (id) {
            if (!window.confirm('このメッセージを自分の画面からだけ消します。相手の画面には残ります。よろしいですか？')) { return; }
            fetch('/messages/' + id + '/hide/', {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken() }
            }).then(function (r) { return r.json(); }).then(function (d) {
                if (d && d.ok) {
                    var bubble = log.querySelector('[data-message-id="' + id + '"]');
                    if (bubble) {
                        var row = bubble.closest('.bubble-row');
                        if (row) { row.remove(); } else { bubble.remove(); }
                    }
                    var holder = log.querySelector('[data-reactions-for="' + id + '"]');
                    if (holder) { holder.remove(); }
                    toast('自分の画面から消しました。', '', 'info');
                }
            }).catch(function () { toast('消せませんでした。', '', 'warning'); });
        };
        var copyMessage = function (id) {
            var bubble = log.querySelector('[data-message-id="' + id + '"]');
            var textEl = bubble ? bubble.querySelector('.bubble-text') : null;
            var text = textEl ? textEl.textContent : '';
            if (!text) { toast('コピーできるテキストがありません。', '', 'warning'); return; }
            var done = function () { toast('コピーしました。', '', 'info'); };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(done).catch(function () { fallbackCopy(text); done(); });
            } else {
                fallbackCopy(text);
                done();
            }
        };
        document.addEventListener('click', function (e) {
            if (!reactionBar.hidden && !reactionBar.contains(e.target)) { reactionBar.hidden = true; }
        });
        var sendReaction = function (messageId, emoji) {
            if (window.__chatSendReaction) { window.__chatSendReaction(messageId, emoji); return; }
            fetch('/messages/' + messageId + '/react/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
                body: JSON.stringify({ emoji: emoji })
            }).then(function (r) { return r.json(); }).then(function (d) {
                if (d && d.reactions) {
                    var mine = d.reactions.map(function (r) {
                        return { emoji: r.emoji, count: r.count, user_ids: r.mine ? [me] : [] };
                    });
                    updateReactions(messageId, mine);
                }
            }).catch(function () {});
        };
        var csrfToken = function () {
            var m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
            return m ? decodeURIComponent(m[1]) : '';
        };
        // 長押し(または右クリック)でリアクションバーを開く
        var pressTimer = null;
        var openBarFor = function (bubble, x, y) {
            reactionBar.dataset.messageId = bubble.dataset.messageId;
            reactionBar.hidden = false;
            var w = reactionBar.offsetWidth || 300;
            reactionBar.style.left = Math.max(8, Math.min(x - 40, window.innerWidth - w - 8)) + 'px';
            reactionBar.style.top = Math.max(8, y - 150) + 'px';
        };
        log.addEventListener('pointerdown', function (e) {
            var bubble = e.target.closest('[data-message-id]');
            if (!bubble) { return; }
            var x = e.clientX, y = e.clientY;
            pressTimer = setTimeout(function () { openBarFor(bubble, x, y); }, 500);
        });
        ['pointerup', 'pointermove', 'pointercancel'].forEach(function (ev) {
            log.addEventListener(ev, function () { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } });
        });
        log.addEventListener('contextmenu', function (e) {
            var bubble = e.target.closest('[data-message-id]');
            if (bubble) { e.preventDefault(); openBarFor(bubble, e.clientX, e.clientY); }
        });
        log.addEventListener('click', function (e) {
            var quote = e.target.closest('.bubble-quote');
            if (quote) {
                var target = log.querySelector('[data-message-id="' + quote.dataset.quoteTarget + '"]');
                if (target) {
                    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
                    target.classList.add('bubble-highlight');
                    setTimeout(function () { target.classList.remove('bubble-highlight'); }, 1400);
                }
                return;
            }
            var chip = e.target.closest('.reaction-chip');
            if (chip) {
                var holder = chip.closest('[data-reactions-for]');
                if (holder) { sendReaction(Number(holder.dataset.reactionsFor), chip.dataset.emoji); }
            }
        });

        var appendMessage = function (m) {
            if (seen[m.id]) { return; }
            seen[m.id] = true;
            var mine = m.sender_id === me;
            if (m.hidden && !mine) { return; }
            var row = document.createElement('div');
            row.className = 'bubble-row' + (mine ? ' mine' : '');
            var bubble = document.createElement('div');
            bubble.className = 'bubble' + (m.hidden ? ' bubble-hidden' : '');
            bubble.dataset.messageId = m.id;
            bubble.dataset.senderName = m.sender || '';
            var reactionsEl = document.createElement('div');
            reactionsEl.className = 'reactions';
            reactionsEl.dataset.reactionsFor = m.id;
            renderReactions(reactionsEl, m.reactions || []);
            if (m.hidden) {
                bubble.textContent = 'このメッセージは相手に表示されていません';
            } else {
                if (m.reply_to) {
                    var quoteEl = document.createElement('div');
                    quoteEl.className = 'bubble-quote';
                    quoteEl.dataset.quoteTarget = m.reply_to.id;
                    var qName = document.createElement('span');
                    qName.className = 'bubble-quote-name';
                    qName.textContent = m.reply_to.sender;
                    var qText = document.createElement('span');
                    qText.className = 'bubble-quote-text';
                    qText.textContent = m.reply_to.excerpt;
                    quoteEl.appendChild(qName);
                    quoteEl.appendChild(qText);
                    bubble.appendChild(quoteEl);
                }
                if (m.image_url) {
                    var img = document.createElement('img');
                    img.className = 'bubble-img';
                    img.src = m.image_url;
                    img.alt = '送信された写真';
                    img.loading = 'lazy';
                    bubble.appendChild(img);
                }
                if (m.stamp_emoji) {
                    var stampEl = document.createElement('span');
                    stampEl.className = 'bubble-stamp';
                    var stampEmoji = document.createElement('span');
                    stampEmoji.className = 'bubble-stamp-emoji';
                    stampEmoji.textContent = m.stamp_emoji;
                    var stampLabel = document.createElement('span');
                    stampLabel.className = 'bubble-stamp-label';
                    stampLabel.textContent = m.stamp_label || '';
                    stampEl.appendChild(stampEmoji);
                    stampEl.appendChild(stampLabel);
                    bubble.appendChild(stampEl);
                }
                if (m.body) {
                    var bodySpan = document.createElement('span');
                    bodySpan.className = 'bubble-text';
                    bodySpan.textContent = m.body;
                    bubble.appendChild(bodySpan);
                }
            }
            var meta = document.createElement('span');
            meta.className = 'bubble-meta';
            meta.textContent = m.created_at;
            if (!mine) {
                var report = document.createElement('a');
                report.href = (m.report_url || log.dataset.reportUrl) + '?message=' + m.id;
                report.textContent = '通報';
                meta.appendChild(document.createTextNode(' · '));
                meta.appendChild(report);
            }
            if (log.dataset.group === '1' && !mine) {
                // グループチャット: アイコンと名前を付ける
                var side = document.createElement('div');
                side.className = 'bubble-side';
                var av = document.createElement('img');
                av.className = 'chat-avatar';
                av.src = m.avatar_url || '';
                av.alt = '';
                av.loading = 'lazy';
                av.addEventListener('error', function () { av.style.visibility = 'hidden'; });
                if (!m.avatar_url) { av.style.visibility = 'hidden'; }
                var main = document.createElement('div');
                main.className = 'bubble-main';
                var nm = document.createElement('span');
                nm.className = 'bubble-name';
                nm.textContent = m.sender;
                main.appendChild(nm);
                main.appendChild(bubble);
                main.appendChild(reactionsEl);
                main.appendChild(meta);
                side.appendChild(av);
                side.appendChild(main);
                row.appendChild(side);
            } else {
                row.appendChild(bubble);
                row.appendChild(reactionsEl);
                row.appendChild(meta);
            }
            log.appendChild(row);
            scrollDown();
            if (!mine && document.visibilityState === 'visible') { chat.send({ type: 'read' }); }
        };

        var markAllMineRead = function () {
            var metas = log.querySelectorAll('.bubble-row.mine .bubble-meta');
            for (var i = 0; i < metas.length; i++) {
                if (!metas[i].querySelector('.read-mark')) {
                    var s = document.createElement('span');
                    s.className = 'read-mark';
                    s.textContent = '既読';
                    metas[i].appendChild(document.createTextNode(' · '));
                    metas[i].appendChild(s);
                }
            }
        };

        var chat = connect('/ws/chat/' + log.dataset.chatId + '/', {
            message: function (data) {
                if (data.error) { toast(data.error, '', 'warning'); return; }
                if (data.kind === 'read') { if (data.reader_id !== me) { markAllMineRead(); } return; }
                if (data.kind === 'reaction_update') { updateReactions(data.message_id, data.reactions); return; }
                appendMessage(data.message);
            }
        });
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') { chat.send({ type: 'read' }); }
        });
        window.__chatSendReaction = function (messageId, emoji) {
            chat.send({ type: 'reaction', message_id: messageId, emoji: emoji });
        };
        if (form) {
            var bodyInput = form.querySelector('input[name=body]');
            var photoInput = form.querySelector('input[name=image]');
            if (photoInput) {
                photoInput.addEventListener('change', function () {
                    var file = photoInput.files && photoInput.files[0];
                    if (!file) { return; }
                    if (file.size > 8 * 1024 * 1024) {
                        toast('写真は8MB以下にしてください。', '', 'warning');
                        photoInput.value = '';
                        return;
                    }
                    var fd = new FormData();
                    fd.append('csrfmiddlewaretoken', form.querySelector('[name=csrfmiddlewaretoken]').value);
                    fd.append('body', '');
                    if (replyToId) { fd.append('reply_to', replyToId); }
                    fd.append('image', file);
                    clearReply();
                    fetch(location.pathname, { method: 'POST', body: fd, credentials: 'same-origin' })
                        .then(function (r) { if (!r.ok) { throw new Error('send failed'); } })
                        .catch(function () { toast('写真を送信できませんでした。もう一度お試しください。', '', 'warning'); });
                    photoInput.value = '';
                });
            }
            var stampPanel = document.getElementById('stamp-panel');
            var stampToggle = document.getElementById('stamp-toggle');
            if (stampPanel && stampToggle) {
                stampToggle.addEventListener('click', function () {
                    stampPanel.hidden = !stampPanel.hidden;
                    if (!stampPanel.hidden) { scrollDown(); }
                });
                stampPanel.querySelectorAll('[data-stamp]').forEach(function (btn) {
                    btn.addEventListener('click', function () {
                        var key = btn.dataset.stamp;
                        stampPanel.hidden = true;
                        if (chat.send({ stamp: key, body: '' })) { return; }
                        // 接続できないときは通常のPOSTで送る(写真と同じ方式)
                        var fd = new FormData();
                        fd.append('csrfmiddlewaretoken', form.querySelector('[name=csrfmiddlewaretoken]').value);
                        fd.append('body', '');
                        fd.append('stamp', key);
                        if (replyToId) { fd.append('reply_to', replyToId); }
                        clearReply();
                        fetch(location.pathname, { method: 'POST', body: fd, credentials: 'same-origin' })
                            .then(function (r) { if (!r.ok) { throw new Error('send failed'); } })
                            .catch(function () { toast('スタンプを送信できませんでした。', '', 'warning'); });
                    });
                });
            }
            form.addEventListener('submit', function (e) {
                var body = bodyInput.value.trim();
                if (!body) { e.preventDefault(); return; }
                if (chat.send({ body: body, reply_to: replyToId })) {  // 接続できないときは通常のフォーム送信にフォールバック
                    e.preventDefault();
                    bodyInput.value = '';
                    clearReply();
                }
            });

            /* スマホのソフトウェアキーボードで入力欄が隠れないようにする。
               キーボードの高さ分だけ入力バーを上に逃がし、下部タブを隠す。 */
            var vv = window.visualViewport;
            var syncKb = function () {
                var inset = 0;
                if (vv) { inset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop); }
                document.documentElement.style.setProperty('--kb-inset', inset + 'px');
                if (inset > 60) { scrollDown(); }
            };
            if (vv) {
                vv.addEventListener('resize', syncKb);
                vv.addEventListener('scroll', syncKb);
            }
            bodyInput.addEventListener('focus', function () {
                syncKb();
                setTimeout(scrollDown, 350);
            });
            bodyInput.addEventListener('blur', function () {
                document.documentElement.style.setProperty('--kb-inset', '0px');
            });
        }
        scrollDown();
    }
})();

/* フォームの二重送信防止(POSTフォーム共通)
   送信後はボタンを無効化して「送信中…」にし、同じフォームの再送信を止める。
   WebSocket チャットのように他のハンドラが送信を取り消した場合は何もしない。 */
(function () {
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!(form instanceof HTMLFormElement)) { return; }
        if ((form.method || 'get').toLowerCase() !== 'post') { return; }
        if (form.dataset.submitting === '1') { e.preventDefault(); return; }
        setTimeout(function () {
            if (e.defaultPrevented) { return; }
            form.dataset.submitting = '1';
            var buttons = form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]');
            buttons.forEach(function (btn) {
                if (btn.tagName === 'BUTTON' && btn.children.length === 0 && btn.textContent.trim()) {
                    btn.dataset.origText = btn.textContent;
                    btn.textContent = '送信中…';
                }
                btn.disabled = true;
            });
            setTimeout(function () {
                delete form.dataset.submitting;
                buttons.forEach(function (btn) {
                    btn.disabled = false;
                    if (btn.dataset.origText) { btn.textContent = btn.dataset.origText; delete btn.dataset.origText; }
                });
            }, 15000);
        }, 0);
    });
})();


/* PWA: Service Worker の登録(ホーム画面への追加に必要) */
(function () {
    if (!('serviceWorker' in navigator)) { return; }
    if (location.protocol !== 'https:' && location.hostname !== 'localhost') { return; }
    window.addEventListener('load', function () {
        navigator.serviceWorker.register('/sw.js').catch(function () {});
    });
})();

/* パスワード欄の表示/非表示ボタン(目のマーク)。ログイン・新規登録・変更など全画面共通。 */
(function () {
    var EYE = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
    var EYE_OFF = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3l18 18"/><path d="M10.6 5.1A9.8 9.8 0 0 1 12 5c6.5 0 10 7 10 7a17.5 17.5 0 0 1-2.9 3.6M6.6 6.9A16.9 16.9 0 0 0 2 12s3.5 7 10 7c1.5 0 2.9-.3 4.1-.8"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';
    document.querySelectorAll('input[type="password"]').forEach(function (input) {
        var wrap = document.createElement('span');
        wrap.className = 'pw-wrap';
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'pw-toggle';
        btn.setAttribute('aria-label', 'パスワードを表示');
        btn.innerHTML = EYE_OFF;
        btn.addEventListener('click', function () {
            var show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            btn.innerHTML = show ? EYE : EYE_OFF;
            btn.setAttribute('aria-label', show ? 'パスワードを隠す' : 'パスワードを表示');
            input.focus();
        });
        wrap.appendChild(btn);
    });
})();

/* 外部アカウントのボタンを統一デザインにする(Google 公式風: 白ボタン+Gアイコン / LINE: 緑ボタン) */
(function () {
    var GOOGLE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#4285F4" d="M23.5 12.3c0-.9-.1-1.5-.3-2.2H12v4.3h6.5c-.1 1.1-.8 2.7-2.4 3.8l3.7 2.9c2.2-2.1 3.7-5.2 3.7-8.8z"/><path fill="#34A853" d="M12 24c3.2 0 5.9-1.1 7.9-2.9l-3.7-2.9c-1 .7-2.4 1.2-4.2 1.2-3.2 0-5.9-2.1-6.8-5l-3.9 3C3.3 21.3 7.3 24 12 24z"/><path fill="#FBBC05" d="M5.2 14.4c-.2-.7-.4-1.5-.4-2.4s.1-1.7.4-2.4l-3.9-3C.5 8.2 0 10 0 12s.5 3.8 1.3 5.4l3.9-3z"/><path fill="#EA4335" d="M12 4.7c1.8 0 3 .8 3.7 1.4l3.3-3.2C17.9 1.1 15.2 0 12 0 7.3 0 3.3 2.7 1.3 6.6l3.9 3c.9-2.8 3.6-4.9 6.8-4.9z"/></svg>';
    var LINE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M12 3C6.4 3 2 6.7 2 11.2c0 4 3.6 7.4 8.4 8 .3 0 .7.2.8.6l.4 1.5c.1.5.6.3.8 0l1.7-2.1c.2-.3.5-.4.9-.5 3.9-.5 7-3.5 7-6.5C22 6.7 17.6 3 12 3z"/><text x="12" y="13.6" text-anchor="middle" font-size="5" font-weight="bold" fill="#06C755" font-family="Arial, sans-serif">LINE</text></svg>';
    var defs = [
        { needle: '/accounts/google/login/', cls: 'social-btn-google', icon: GOOGLE_ICON, name: 'Googleアカウント' },
        { needle: '/accounts/line/login/', cls: 'social-btn-line', icon: LINE_ICON, name: 'LINEアカウント' }
    ];
    var mode = location.pathname.indexOf('signup') !== -1 ? 'で登録' : 'でログイン';
    document.querySelectorAll('a[href*="/accounts/google/login/"], a[href*="/accounts/line/login/"]').forEach(function (a) {
        var def = defs.filter(function (d) { return a.getAttribute('href').indexOf(d.needle) !== -1; })[0];
        if (!def) { return; }
        var suffix = a.getAttribute('href').indexOf('process=connect') !== -1 ? 'と連携' : mode;
        a.classList.add('social-btn', def.cls);
        a.innerHTML = '';
        var icon = document.createElement('span');
        icon.className = 'social-btn-icon';
        icon.innerHTML = def.icon;
        var label = document.createElement('span');
        label.textContent = def.name + suffix;
        a.appendChild(icon);
        a.appendChild(label);
    });
})();

/* 顔写真のトリミング(位置・拡大を自由に調整して正方形に切り取る) */
(function () {
    var input = document.querySelector('form input[type="file"][name="photo"]');
    var modal = document.getElementById('crop-modal');
    if (!input || !modal) { return; }
    var viewport = document.getElementById('crop-viewport');
    var img = document.getElementById('crop-img');
    var zoom = document.getElementById('crop-zoom');
    var state = { base: 1, scale: 1, x: 0, y: 0, url: null };
    var clampPos = function () {
        var vw = viewport.clientWidth;
        var w = img.naturalWidth * state.base * state.scale;
        var h = img.naturalHeight * state.base * state.scale;
        state.x = Math.min(0, Math.max(vw - w, state.x));
        state.y = Math.min(0, Math.max(vw - h, state.y));
    };
    var draw = function () {
        var w = img.naturalWidth * state.base * state.scale;
        var h = img.naturalHeight * state.base * state.scale;
        img.style.width = w + 'px';
        img.style.height = h + 'px';
        img.style.transform = 'translate(' + state.x + 'px,' + state.y + 'px)';
    };
    var open = function (file) {
        if (state.url) { URL.revokeObjectURL(state.url); }
        state.url = URL.createObjectURL(file);
        img.onload = function () {
            var vw = viewport.clientWidth || 300;
            state.base = Math.max(vw / img.naturalWidth, vw / img.naturalHeight);
            state.scale = 1;
            zoom.value = '1';
            state.x = (vw - img.naturalWidth * state.base) / 2;
            state.y = (vw - img.naturalHeight * state.base) / 2;
            draw();
        };
        img.src = state.url;
        modal.hidden = false;
    };
    input.addEventListener('change', function () {
        var file = input.files && input.files[0];
        if (file && /^image\//.test(file.type)) { open(file); }
    });
    var drag = null;
    viewport.addEventListener('pointerdown', function (e) {
        drag = { x: e.clientX, y: e.clientY, ox: state.x, oy: state.y };
        viewport.setPointerCapture(e.pointerId);
    });
    viewport.addEventListener('pointermove', function (e) {
        if (!drag) { return; }
        state.x = drag.ox + (e.clientX - drag.x);
        state.y = drag.oy + (e.clientY - drag.y);
        clampPos(); draw();
    });
    ['pointerup', 'pointercancel'].forEach(function (ev) {
        viewport.addEventListener(ev, function () { drag = null; });
    });
    var pinch = null;
    viewport.addEventListener('touchmove', function (e) {
        if (e.touches.length === 2) {
            e.preventDefault();
            var d = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
            if (pinch) {
                state.scale = Math.min(3, Math.max(1, state.scale * d / pinch));
                zoom.value = String(state.scale);
                clampPos(); draw();
            }
            pinch = d;
        }
    }, { passive: false });
    viewport.addEventListener('touchend', function () { pinch = null; });
    zoom.addEventListener('input', function () {
        var vw = viewport.clientWidth;
        var cx = (vw / 2 - state.x) / state.scale, cy = (vw / 2 - state.y) / state.scale;
        state.scale = Number(zoom.value);
        state.x = vw / 2 - cx * state.scale;
        state.y = vw / 2 - cy * state.scale;
        clampPos(); draw();
    });
    document.getElementById('crop-cancel').addEventListener('click', function () {
        modal.hidden = true;
        input.value = '';
    });
    document.getElementById('crop-apply').addEventListener('click', function () {
        var vw = viewport.clientWidth;
        var k = state.base * state.scale;
        var canvas = document.createElement('canvas');
        canvas.width = canvas.height = 640;
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#fff';
        ctx.fillRect(0, 0, 640, 640);
        ctx.drawImage(img, state.x / vw * 640, state.y / vw * 640,
                      img.naturalWidth * k / vw * 640, img.naturalHeight * k / vw * 640);
        canvas.toBlob(function (blob) {
            if (!blob) { modal.hidden = true; return; }
            var file = new File([blob], 'photo.jpg', { type: 'image/jpeg' });
            var dt = new DataTransfer();
            dt.items.add(file);
            input.files = dt.files;
            modal.hidden = true;
            var preview = document.querySelector('.photo-edit .avatar-img, .photo-edit img');
            if (preview) { preview.src = canvas.toDataURL('image/jpeg', 0.85); }
        }, 'image/jpeg', 0.9);
    });
})();

/* Web Push: 購読の登録(許可済みなら自動で最新化、設定画面ではボタンで切り替え) */
(function () {
    var meta = document.querySelector('meta[name="vapid-public-key"]');
    var key = meta ? meta.getAttribute('content') : '';
    /* 通知オプトインカード: 登録直後などの最初の画面で自動表示する。
       iOS の仕様上、許可ダイアログ自体はボタンのタップからしか出せない。 */
    var prompt = document.getElementById('push-prompt');
    var DISMISS_KEY = 'sukiPushPromptDismissed';
    var isDismissed = function () { try { return localStorage.getItem(DISMISS_KEY) === '1'; } catch (e) { return false; } };
    var markDismissed = function () { try { localStorage.setItem(DISMISS_KEY, '1'); } catch (e) {} };
    var supported = !!(key && ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window));
    if (prompt && !supported && !isDismissed()) {
        // iPhone の Safari タブでは通知が使えないため、ホーム画面への追加を案内する
        var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
        var standalone = window.navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches;
        if (isIOS && !standalone) {
            var guideText = prompt.querySelector('[data-push-text]');
            if (guideText) { guideText.textContent = 'iPhoneで通知を受け取るには、このページをホーム画面に追加してください。共有ボタンから「ホーム画面に追加」を選んで、ホーム画面のアイコンから開くと通知をオンにできます。'; }
            var guideEnable = prompt.querySelector('[data-push-enable]');
            if (guideEnable) { guideEnable.hidden = true; }
            var guideDismiss = prompt.querySelector('[data-push-dismiss]');
            if (guideDismiss) {
                guideDismiss.textContent = 'わかった';
                guideDismiss.addEventListener('click', function () { markDismissed(); prompt.hidden = true; });
            }
            prompt.hidden = false;
        }
    }
    if (!supported) { return; }
    var csrf = function () {
        var m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
        return m ? decodeURIComponent(m[1]) : '';
    };
    var toKey = function (base64) {
        var pad = '='.repeat((4 - base64.length % 4) % 4);
        var raw = atob((base64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
        var arr = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i++) { arr[i] = raw.charCodeAt(i); }
        return arr;
    };
    var post = function (path, sub) {
        return fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
            body: JSON.stringify(sub ? sub.toJSON() : {})
        });
    };
    var subscribe = function () {
        return navigator.serviceWorker.ready.then(function (reg) {
            return reg.pushManager.getSubscription().then(function (existing) {
                if (existing) { return existing; }
                return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: toKey(key) });
            });
        }).then(function (sub) { return post('/push/subscribe/', sub); });
    };
    if (Notification.permission === 'granted') { subscribe().catch(function () {}); }
    if (prompt && !isDismissed() && Notification.permission === 'default') {
        navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); })
            .then(function (sub) { if (!sub) { prompt.hidden = false; } }).catch(function () {});
        var enableBtn = prompt.querySelector('[data-push-enable]');
        if (enableBtn) {
            enableBtn.addEventListener('click', function () {
                Notification.requestPermission().then(function (perm) {
                    markDismissed();
                    prompt.hidden = true;
                    if (perm !== 'granted') { toast('通知はオンになりませんでした。あとから設定画面で変更できます。', '', 'warning'); return; }
                    return subscribe().then(function () { toast('プッシュ通知を有効にしました。', '', 'info'); });
                }).catch(function () { prompt.hidden = true; });
            });
        }
        var dismissBtn = prompt.querySelector('[data-push-dismiss]');
        if (dismissBtn) { dismissBtn.addEventListener('click', function () { markDismissed(); prompt.hidden = true; }); }
    }
    var btn = document.getElementById('push-toggle');
    if (btn) {
        var refreshLabel = function () {
            navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); })
                .then(function (sub) {
                    btn.textContent = sub ? 'この端末でプッシュ通知をオフにする' : 'この端末でプッシュ通知を有効にする';
                }).catch(function () {});
        };
        refreshLabel();
        btn.addEventListener('click', function () {
            navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); })
                .then(function (sub) {
                    if (sub) {
                        return post('/push/unsubscribe/', sub).then(function () { return sub.unsubscribe(); })
                            .then(function () { toast('プッシュ通知をオフにしました。', '', 'info'); refreshLabel(); });
                    }
                    return Notification.requestPermission().then(function (perm) {
                        if (perm !== 'granted') { toast('通知が許可されませんでした。ブラウザの設定を確認してください。', '', 'warning'); return; }
                        return subscribe().then(function () { toast('プッシュ通知を有効にしました。', '', 'info'); refreshLabel(); });
                    });
                }).catch(function () { toast('プッシュ通知の設定に失敗しました。', '', 'warning'); });
        });
    }
})();
// コピーボタン(data-copy="<コピー元要素のセレクタ>")
document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
        var el = document.querySelector(btn.getAttribute('data-copy'));
        if (!el) return;
        var text = (el.value !== undefined && el.value !== '') ? el.value : el.textContent.trim();
        var done = function () {
            var original = btn.textContent;
            btn.textContent = 'コピーしました ✓';
            btn.disabled = true;
            setTimeout(function () { btn.textContent = original; btn.disabled = false; }, 2000);
        };
        var fallback = function () {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); } catch (e) {}
            ta.remove();
            done();
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(fallback);
        } else {
            fallback();
        }
    });
});
// 依頼フォーム: 選んだ写真のプレビュー
(function () {
    var input = document.getElementById('id_image');
    var preview = document.querySelector('[data-photo-preview]');
    if (!input || !preview) return;
    input.addEventListener('change', function () {
        var file = input.files && input.files[0];
        if (file) {
            preview.src = URL.createObjectURL(file);
            preview.hidden = false;
        } else {
            preview.hidden = true;
        }
    });
})();
// ---------- トーク一覧: 長押しでトークを消す(自分の画面だけ) ----------
(function () {
    var rows = document.querySelectorAll('[data-talk-hide]');
    if (!rows.length) { return; }
    var csrf = function () {
        var m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
        return m ? decodeURIComponent(m[1]) : '';
    };
    rows.forEach(function (row) {
        var timer = null;
        var suppress = false;
        row.addEventListener('pointerdown', function () {
            timer = setTimeout(function () {
                suppress = true;
                setTimeout(function () { suppress = false; }, 600);
                if (!window.confirm('このトークを自分の画面から消しますか？相手の画面には影響しません。新しいメッセージが来ると、また表示されます。')) { return; }
                var ids = row.dataset.talkHide.split(',').filter(Boolean);
                (async function () {
                    for (var i = 0; i < ids.length; i++) {
                        await fetch('/talks/' + ids[i] + '/hide/', {
                            method: 'POST',
                            headers: { 'X-CSRFToken': csrf() }
                        });
                    }
                    location.reload();
                })();
            }, 550);
        });
        ['pointerup', 'pointermove', 'pointercancel'].forEach(function (ev) {
            row.addEventListener(ev, function () { if (timer) { clearTimeout(timer); timer = null; } });
        });
        row.addEventListener('click', function (e) { if (suppress) { e.preventDefault(); } });
        row.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    });
})();
// ---------- チャットの長押しヒント(閉じたら端末に記憶) ----------
(function () {
    var hint = document.getElementById('chat-hint');
    if (!hint) { return; }
    var KEY = 'sukiChatHintDismissed';
    try { if (localStorage.getItem(KEY) === '1') { return; } } catch (e) {}
    hint.hidden = false;
    var btn = document.getElementById('chat-hint-close');
    if (btn) {
        btn.addEventListener('click', function () {
            hint.hidden = true;
            try { localStorage.setItem(KEY, '1'); } catch (e) {}
        });
    }
})();
