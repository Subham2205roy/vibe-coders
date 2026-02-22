// ── SmartTransit App ─────────────────────────────────────────────────
const API = 'http://127.0.0.1:8000';
let ORS_KEY = '';
let GEMINI_KEY = '';
let GOOGLE_CLIENT_ID = '';

let currentUser = null;
let authToken = localStorage.getItem('st_token') || null;
let map = null, journeyMap = null, journeyLine = null;
let trackMap = null, trackMarker = null, trackInterval = null;
let busMarkers = {};
let busRefreshInterval = null;
let locationWatchId = null;
let tripTimer = null, tripSeconds = 0, tripCoords = [], tripDist = 0;
let paxCount = 0;
let chatHistory = [];
let _googleRole = 'passenger';

// ── Initialization ──────────────────────────────────────────────────
async function initApp() {
    try {
        const r = await fetch(`${API}/config`);
        const d = await r.json();
        GOOGLE_CLIENT_ID = d.google_client_id;
        ORS_KEY = d.ors_key;
        GEMINI_KEY = d.gemini_key;
        console.log('Config loaded');
    } catch (e) {
        console.error('Failed to load config:', e);
        toast('Failed to load application configuration. Some features may not work.', 'error');
    }
}
initApp();

// ── Google OAuth ─────────────────────────────────────────────────────
function googleSignIn(role) {
    _googleRole = role;
    if (typeof google === 'undefined' || !google.accounts) {
        toast('Google Sign-In SDK not loaded yet. Please wait a moment and try again.', 'error');
        return;
    }
    google.accounts.id.initialize({
        client_id: GOOGLE_CLIENT_ID,
        callback: handleGoogleCallback,
    });
    google.accounts.id.prompt((notification) => {
        // If One Tap is suppressed (e.g. cooldown), fall back to a button-rendered popup
        if (notification.isNotDisplayed() || notification.isSkippedMoment()) {
            // Use the popup approach
            google.accounts.id.renderButton(
                document.createElement('div'), // dummy element
                { type: 'standard' }
            );
            // Trigger the popup sign-in
            google.accounts.oauth2.initTokenClient({
                client_id: GOOGLE_CLIENT_ID,
                scope: 'email profile',
                callback: () => { },
            });
            // Alternative: use prompt again or show manual message
            toast('Please allow popups for Google Sign-In, or try again.', 'info');
        }
    });
}

async function handleGoogleCallback(response) {
    if (!response.credential) {
        toast('Google Sign-In failed', 'error');
        return;
    }
    try {
        const r = await fetch(`${API}/auth/google`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ credential: response.credential, role: _googleRole })
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || 'Google login failed');
        authToken = d.token;
        currentUser = d.user;
        localStorage.setItem('st_token', authToken);
        toast(`Welcome, ${d.user.name}!`, 'success');
        showPage(_googleRole === 'driver' ? 'driver-dashboard' : 'passenger-dashboard');
    } catch (e) {
        toast(e.message, 'error');
    }
}

// ── Toast ────────────────────────────────────────────────────────────
function toast(msg, type = 'info') {
    const c = document.getElementById('toast-container');
    const icons = { success: 'fa-check-circle', error: 'fa-exclamation-circle', info: 'fa-info-circle' };
    const t = document.createElement('div');
    t.className = `toast toast-${type}`;
    t.innerHTML = `<i class="fas ${icons[type] || icons.info}"></i><span>${msg}</span>`;
    c.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transform = 'translateX(40px)'; setTimeout(() => t.remove(), 300); }, 3500);
}

// ── Dark Mode ────────────────────────────────────────────────────────
function toggleDarkMode() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    document.documentElement.setAttribute('data-theme', isDark ? '' : 'dark');
    localStorage.setItem('st_dark', isDark ? '' : 'dark');
    document.getElementById('dark-toggle').innerHTML = isDark ? '<i class="fas fa-moon"></i>' : '<i class="fas fa-sun"></i>';
}
if (localStorage.getItem('st_dark') === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    document.getElementById('dark-toggle').innerHTML = '<i class="fas fa-sun"></i>';
}

// ── Navigation ───────────────────────────────────────────────────────
function showPage(id) {
    document.querySelectorAll('.page').forEach(p => { p.classList.remove('active'); p.style.display = 'none'; });
    const page = document.getElementById(id);
    if (page) { page.style.display = 'block'; page.classList.add('active'); }
    // Hooks
    if (id === 'passenger-dashboard') loadDashboard();
    if (id === 'driver-dashboard') loadDriverDashboard();
    if (id === 'live-map-page') initMap();
    if (id === 'track-bus-page') initTrackMap();
    if (id === 'crowd-page') loadCrowdLevels();
    if (id === 'tickets-page') loadTickets();
    if (id === 'saved-routes-page') loadSavedRoutes();
    if (id === 'ai-chat-page') initChat();
}

// ── Auth ──────────────────────────────────────────────────────────────
function toggleAuthForm(role, mode) {
    const prefix = role === 'passenger' ? 'p' : 'd';
    document.getElementById(`${prefix}-login-form`).classList.toggle('hidden', mode !== 'login');
    document.getElementById(`${prefix}-register-form`).classList.toggle('hidden', mode !== 'register');
}

async function passengerLogin() {
    const email = document.getElementById('p-login-email').value.trim();
    const pass = document.getElementById('p-login-pass').value;
    if (!email || !pass) return toast('Please fill all fields', 'error');
    try {
        const r = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password: pass }) });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || 'Login failed');
        authToken = d.token; currentUser = d.user;
        localStorage.setItem('st_token', authToken);
        toast(`Welcome back, ${d.user.name}!`, 'success');
        showPage('passenger-dashboard');
    } catch (e) { toast(e.message, 'error'); }
}

async function passengerRegister() {
    const name = document.getElementById('p-reg-name').value.trim();
    const email = document.getElementById('p-reg-email').value.trim();
    const phone = document.getElementById('p-reg-phone').value.trim();
    const pass = document.getElementById('p-reg-pass').value;
    if (!name || !email || !pass) return toast('Please fill all required fields', 'error');
    try {
        const r = await fetch(`${API}/auth/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, email, password: pass, phone, role: 'passenger' }) });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || 'Registration failed');
        authToken = d.token; currentUser = d.user;
        localStorage.setItem('st_token', authToken);
        toast('Registration successful!', 'success');
        showPage('passenger-dashboard');
    } catch (e) { toast(e.message, 'error'); }
}

async function driverLogin() {
    const email = document.getElementById('d-login-email').value.trim();
    const pass = document.getElementById('d-login-pass').value;
    if (!email || !pass) return toast('Please fill all fields', 'error');
    try {
        const r = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password: pass }) });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || 'Login failed');
        authToken = d.token; currentUser = d.user;
        localStorage.setItem('st_token', authToken);
        toast(`Welcome, ${d.user.name}!`, 'success');
        showPage('driver-dashboard');
    } catch (e) { toast(e.message, 'error'); }
}

async function driverRegister() {
    const name = document.getElementById('d-reg-name').value.trim();
    const email = document.getElementById('d-reg-email').value.trim();
    const empid = document.getElementById('d-reg-empid').value.trim();
    const pass = document.getElementById('d-reg-pass').value;
    if (!name || !email || !pass || !empid) return toast('Please fill all fields', 'error');
    try {
        const r = await fetch(`${API}/auth/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, email, password: pass, employee_id: empid, role: 'driver' }) });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || 'Registration failed');
        toast('Registration successful! Please login.', 'success');
        toggleAuthForm('driver', 'login');
    } catch (e) { toast(e.message, 'error'); }
}

function passengerLogout() { authToken = null; currentUser = null; localStorage.removeItem('st_token'); clearIntervals(); showPage('landing-page'); toast('Logged out', 'info'); }
function driverLogout() { authToken = null; currentUser = null; localStorage.removeItem('st_token'); clearIntervals(); showPage('landing-page'); toast('Logged out', 'info'); }

function clearIntervals() {
    if (busRefreshInterval) clearInterval(busRefreshInterval);
    if (locationWatchId) navigator.geolocation.clearWatch(locationWatchId);
    if (tripTimer) clearInterval(tripTimer);
    if (trackInterval) clearInterval(trackInterval);
    busRefreshInterval = null; locationWatchId = null; tripTimer = null; trackInterval = null;
}

// ── Passenger Dashboard ──────────────────────────────────────────────
function loadDashboard() {
    const g = document.getElementById('p-greeting');
    if (currentUser) g.textContent = `Hello, ${currentUser.name}!`;
}

// ── Live Map ─────────────────────────────────────────────────────────
function initMap() {
    if (!map) {
        map = L.map('map').setView([22.5726, 88.3639], 12);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap' }).addTo(map);
    }
    setTimeout(() => map.invalidateSize(), 100);
    loadLiveBuses();
    if (busRefreshInterval) clearInterval(busRefreshInterval);
    busRefreshInterval = setInterval(loadLiveBuses, 8000);
}

async function loadLiveBuses() {
    try {
        const r = await fetch(`${API}/bus/live`);
        const d = await r.json();
        const info = document.getElementById('map-info');
        const listEl = document.getElementById('live-bus-list');
        // Remove old markers not in new data
        const currentIds = d.buses.map(b => b.bus_reg);
        Object.keys(busMarkers).forEach(k => { if (!currentIds.includes(k)) { map.removeLayer(busMarkers[k]); delete busMarkers[k]; } });
        d.buses.forEach(b => {
            const ll = [b.latitude, b.longitude];
            const crowdBadge = b.crowd_level === 'Low' ? 'badge-low' : b.crowd_level === 'Medium' ? 'badge-medium' : 'badge-high';
            const statusBadge = b.status === 'running' ? 'badge-active' : b.status === 'delayed' ? 'badge-delayed' : 'badge-breakdown';
            const popup = `<div style="font-family:Inter,sans-serif;font-size:13px;min-width:180px"><b>${b.bus_reg}</b><br>Route: ${b.route_name || b.route_id || 'N/A'}<br>${b.route_info || ''}<br>Speed: ${b.speed} km/h<br>Crowd: <span class="badge ${crowdBadge}">${b.crowd_level}</span><br>Status: <span class="badge ${statusBadge}">${b.status}</span>${b.delay_reason ? '<br>Reason: ' + b.delay_reason : ''}<br><a href="#" onclick="trackBus('${b.bus_reg}');return false" style="color:#0d9488;font-weight:600">📍 Track This Bus</a></div>`;
            if (busMarkers[b.bus_reg]) {
                busMarkers[b.bus_reg].setLatLng(ll).setPopupContent(popup);
            } else {
                const icon = L.icon({ iconUrl: 'https://img.icons8.com/plasticine/100/bus.png', iconSize: [44, 44] });
                busMarkers[b.bus_reg] = L.marker(ll, { icon }).addTo(map).bindPopup(popup);
            }
        });
        if (info) info.textContent = `${d.buses.length} bus${d.buses.length !== 1 ? 'es' : ''} currently active`;
        // Populate live bus list
        if (listEl) {
            if (!d.buses.length) {
                listEl.innerHTML = '<p style="text-align:center;color:var(--text2);padding:20px">No active buses right now. When a driver starts a trip, their bus will appear here.</p>';
            } else {
                listEl.innerHTML = d.buses.map(b => {
                    const crowdBadge = b.crowd_level === 'Low' ? 'badge-low' : b.crowd_level === 'Medium' ? 'badge-medium' : 'badge-high';
                    const statusBadge = b.status === 'running' ? 'badge-active' : b.status === 'delayed' ? 'badge-delayed' : 'badge-breakdown';
                    return `<div class="result-item" style="display:flex;justify-content:space-between;align-items:center">
                        <div>
                            <h4><i class="fas fa-bus" style="color:var(--accent);margin-right:6px"></i>${b.bus_reg} · ${b.route_name || b.route_id}</h4>
                            <p style="margin:4px 0">${b.route_info || 'No route info'}<br>
                            <span class="badge ${crowdBadge}">${b.crowd_level}</span> <span class="badge ${statusBadge}">${b.status}</span> · ${b.speed} km/h</p>
                        </div>
                        <button class="btn btn-primary btn-sm" onclick="trackBus('${b.bus_reg}')" style="white-space:nowrap"><i class="fas fa-crosshairs"></i> Track</button>
                    </div>`;
                }).join('');
            }
        }
    } catch (e) { console.error(e); }
}

async function findBus() {
    const q = document.getElementById('bus-search-input').value.trim().toUpperCase();
    if (!q) return toast('Enter a bus registration number', 'error');
    try {
        const r = await fetch(`${API}/bus/${q}`);
        if (!r.ok) throw new Error('Bus not found');
        const b = await r.json();
        map.setView([b.latitude, b.longitude], 15);
        if (busMarkers[b.bus_reg]) busMarkers[b.bus_reg].openPopup();
        toast(`Found bus ${q}`, 'success');
    } catch (e) { toast(e.message, 'error'); }
}

// ── Nearest Stops ────────────────────────────────────────────────────
function findNearestStops() {
    if (!navigator.geolocation) return toast('Geolocation not supported', 'error');
    toast('Detecting location...', 'info');
    navigator.geolocation.getCurrentPosition(async (pos) => {
        const { latitude: lat, longitude: lng } = pos.coords;
        try {
            const r = await fetch(`${API}/stops/nearest?lat=${lat}&lng=${lng}&limit=8`);
            const d = await r.json();
            const el = document.getElementById('nearest-results');
            if (!d.stops.length) { el.innerHTML = '<p style="color:var(--text2);text-align:center;padding:20px">No stops found nearby</p>'; return; }
            el.innerHTML = d.stops.map(s => `
                <div class="result-item">
                    <h4><i class="fas fa-map-pin" style="color:var(--accent);margin-right:6px"></i>${s.name}</h4>
                    <p>${s.distance_m}m away · ${s.routes.length} route${s.routes.length !== 1 ? 's' : ''}: ${s.routes.join(', ')}</p>
                </div>`).join('');
            toast(`Found ${d.stops.length} nearby stops`, 'success');
        } catch (e) { toast('Failed to fetch stops', 'error'); }
    }, () => toast('Location access denied', 'error'), { enableHighAccuracy: true });
}

// ── Journey Planner ──────────────────────────────────────────────────
async function planJourney() {
    const from = document.getElementById('journey-from').value.trim();
    const to = document.getElementById('journey-to').value.trim();
    if (!from || !to) return toast('Enter both locations', 'error');
    const el = document.getElementById('journey-results');
    el.innerHTML = '<p style="text-align:center;color:var(--text2);padding:16px"><i class="fas fa-spinner fa-spin"></i> Finding routes...</p>';
    try {
        // Fetch routes AND live buses in parallel
        const [br, lr] = await Promise.all([
            fetch(`${API}/routes/find-route?from_stop=${encodeURIComponent(from)}&to_stop=${encodeURIComponent(to)}`),
            fetch(`${API}/bus/live`).catch(() => null)
        ]);
        // Get live bus data
        let liveBuses = [];
        if (lr && lr.ok) { const ld = await lr.json(); liveBuses = ld.buses || []; }

        if (br.ok) {
            const bd = await br.json();
            if (bd.results && bd.results.length > 0) {
                // Build route results with live bus indicators
                let html = `<div class="result-item" style="border-left-color:var(--success)"><h4>🚌 Public Transport Routes: ${bd.from} → ${bd.to}</h4></div>`;

                const matchedLiveBuses = [];

                html += bd.results.map(r => {
                    if (r.type === 'direct') {
                        // Check if any live bus is on this route
                        const liveBus = liveBuses.find(b => b.route_id === r.route.id && b.status === 'running');
                        if (liveBus) matchedLiveBuses.push(liveBus);
                        const liveBadge = liveBus
                            ? `<span class="badge badge-active" style="margin-left:8px;animation:pulse 2s infinite">🟢 LIVE</span> <button class="btn btn-sm" onclick="trackBus('${liveBus.bus_reg}')" style="background:var(--accent);color:#fff;margin-left:4px;font-size:0.7rem;padding:2px 8px"><i class="fas fa-crosshairs"></i> Track</button>`
                            : '';
                        return `<div class="result-item"><h4><i class="fas fa-bus" style="color:var(--accent)"></i> ${r.route.name} (Direct)${liveBadge}</h4><p>${r.from_stop} → ${r.to_stop}<br>⏱ ~${r.estimated_time_min} min · ₹${r.estimated_fare}${liveBus ? '<br><small style="color:var(--success)">Bus ' + liveBus.bus_reg + ' is running · ' + liveBus.crowd_level + ' crowd · ' + liveBus.speed + ' km/h</small>' : ''}</p></div>`;
                    } else {
                        // Check if live buses exist on either leg
                        const liveBus1 = liveBuses.find(b => b.route_id === r.leg1_route.id && b.status === 'running');
                        const liveBus2 = liveBuses.find(b => b.route_id === r.leg2_route.id && b.status === 'running');
                        if (liveBus1) matchedLiveBuses.push(liveBus1);
                        if (liveBus2) matchedLiveBuses.push(liveBus2);
                        const liveInfo = [];
                        if (liveBus1) liveInfo.push(`${r.leg1_route.name}: Bus ${liveBus1.bus_reg} 🟢`);
                        if (liveBus2) liveInfo.push(`${r.leg2_route.name}: Bus ${liveBus2.bus_reg} 🟢`);
                        return `<div class="result-item" style="border-left-color:var(--warning)"><h4><i class="fas fa-exchange-alt" style="color:var(--warning)"></i> ${r.leg1_route.name} → ${r.leg2_route.name} (1 Transfer)</h4><p>${r.from_stop} → <b>${r.transfer_stop}</b> → ${r.to_stop}<br>⏱ ~${r.estimated_time_min} min · ₹${r.estimated_fare}${liveInfo.length ? '<br><small style="color:var(--success)">' + liveInfo.join(' | ') + '</small>' : ''}</p></div>`;
                    }
                }).join('');

                // Show live buses section if any matched
                if (matchedLiveBuses.length > 0) {
                    const uniqueBuses = [...new Map(matchedLiveBuses.map(b => [b.bus_reg, b])).values()];
                    html += `<div class="result-item" style="border-left-color:var(--success);margin-top:8px"><h4 style="color:var(--success)"><i class="fas fa-broadcast-tower"></i> Live Buses on This Route</h4>`;
                    html += uniqueBuses.map(b => {
                        const crowdBadge = b.crowd_level === 'Low' ? 'badge-low' : b.crowd_level === 'Medium' ? 'badge-medium' : 'badge-high';
                        return `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid rgba(255,255,255,0.06)"><span>${b.bus_reg} · ${b.route_name || b.route_id} · <span class="badge ${crowdBadge}">${b.crowd_level}</span> · ${b.speed} km/h</span><button class="btn btn-sm" onclick="trackBus('${b.bus_reg}')" style="background:var(--accent);color:#fff;font-size:0.7rem"><i class="fas fa-crosshairs"></i> Track</button></div>`;
                    }).join('');
                    html += `</div>`;
                }

                el.innerHTML = html;
                toast(`Found ${bd.results.length} route(s)`, 'success');
                // Use EXACT coordinates from backend database for the map route
                if (bd.from_coords && bd.to_coords) {
                    showOrsRouteByCoords(
                        [bd.from_coords.lng, bd.from_coords.lat],
                        [bd.to_coords.lng, bd.to_coords.lat]
                    );
                }
                return;
            }
        }
        // Fallback: geocode and show road directions when no bus route found
        const sc = await geocode(from), ec = await geocode(to);
        showOrsRouteByCoords(sc, ec);
        el.innerHTML = '<p style="color:var(--text2);padding:12px">No direct bus route found in database. Showing road directions.</p>';
    } catch (e) { el.innerHTML = `<p style="color:var(--danger)">${e.message}</p>`; }
}

async function showOrsRouteByCoords(startCoords, endCoords) {
    // startCoords and endCoords are [lng, lat] arrays (ORS format)
    try {
        const r = await fetch('https://api.openrouteservice.org/v2/directions/driving-car/geojson', {
            method: 'POST', headers: { 'Authorization': ORS_KEY, 'Content-Type': 'application/json' },
            body: JSON.stringify({ coordinates: [startCoords, endCoords] })
        });
        if (!r.ok) return;
        const d = await r.json();
        if (!d.features || !d.features.length) return;
        const mapEl = document.getElementById('journey-map');
        mapEl.style.display = 'block';
        if (!journeyMap) { journeyMap = L.map('journey-map'); L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(journeyMap); }
        setTimeout(() => journeyMap.invalidateSize(), 100);
        if (journeyLine) journeyMap.removeLayer(journeyLine);
        // Remove old markers
        if (window._jmStart) journeyMap.removeLayer(window._jmStart);
        if (window._jmEnd) journeyMap.removeLayer(window._jmEnd);
        // Draw route polyline
        const lls = d.features[0].geometry.coordinates.map(c => [c[1], c[0]]);
        journeyLine = L.polyline(lls, { color: '#0d9488', weight: 5, opacity: 0.85 }).addTo(journeyMap);
        // Add start & end markers
        const startIcon = L.divIcon({ html: '<i class="fas fa-circle" style="color:#0d9488;font-size:14px"></i>', className: '', iconSize: [14, 14] });
        const endIcon = L.divIcon({ html: '<i class="fas fa-map-marker-alt" style="color:#ef4444;font-size:22px"></i>', className: '', iconSize: [22, 22] });
        window._jmStart = L.marker([startCoords[1], startCoords[0]], { icon: startIcon }).addTo(journeyMap);
        window._jmEnd = L.marker([endCoords[1], endCoords[0]], { icon: endIcon }).addTo(journeyMap);
        journeyMap.fitBounds(journeyLine.getBounds(), { padding: [30, 30] });
        const seg = d.features[0].properties.segments[0];
        const el = document.getElementById('journey-results');
        el.innerHTML += `<div class="result-item" style="border-left-color:var(--info);margin-top:8px"><h4>🗺 Road Distance</h4><p>${(seg.distance / 1000).toFixed(1)} km · ~${Math.round(seg.duration / 60)} min drive</p></div>`;
    } catch (e) { console.error(e); }
}

async function geocode(place) {
    // Append ", Kolkata" if user didn't specify a city to avoid Bangladesh/Pakistan results
    const searchText = /kolkata|calcutta|west bengal/i.test(place) ? place : `${place}, Kolkata`;
    // Bounding box: strictly Kolkata metropolitan area (SW lat,lng to NE lat,lng)
    const bbox = 'boundary.rect.min_lon=88.15&boundary.rect.max_lon=88.62&boundary.rect.min_lat=22.25&boundary.rect.max_lat=22.85';
    const r = await fetch(`https://api.openrouteservice.org/geocode/search?api_key=${ORS_KEY}&text=${encodeURIComponent(searchText)}&focus.point.lon=88.3639&focus.point.lat=22.5726&${bbox}&size=3`);
    const d = await r.json();
    if (!d.features?.length) throw new Error(`Cannot find "${place}" in Kolkata area`);
    return d.features[0].geometry.coordinates;
}

// ── Crowd Levels ─────────────────────────────────────────────────────
async function loadCrowdLevels() {
    const el = document.getElementById('crowd-results');
    try {
        const r = await fetch(`${API}/crowd-levels`);
        const d = await r.json();
        if (!d.crowd_data.length) { el.innerHTML = '<p style="text-align:center;color:var(--text2);padding:20px">No active buses right now</p>'; return; }
        el.innerHTML = d.crowd_data.map(b => {
            const badge = b.crowd_level === 'Low' ? 'badge-low' : b.crowd_level === 'Medium' ? 'badge-medium' : 'badge-high';
            const statusBadge = b.status === 'running' ? 'badge-active' : b.status === 'delayed' ? 'badge-delayed' : 'badge-breakdown';
            return `<div class="result-item"><h4>${b.bus_reg} · Route ${b.route_name}</h4><p>${b.route_info}<br><span class="badge ${badge}">${b.crowd_level} (${b.passenger_count} pax)</span> <span class="badge ${statusBadge}">${b.status}</span></p></div>`;
        }).join('');
    } catch (e) { el.innerHTML = '<p style="color:var(--danger)">Failed to load crowd data</p>'; }
}

// ── Tickets ──────────────────────────────────────────────────────────
function showBookTicketForm() {
    const f = document.getElementById('book-ticket-form');
    f.classList.toggle('hidden');
    if (!f.classList.contains('hidden')) loadRouteOptions('ticket-route');
}

async function loadRouteOptions(selectId) {
    try {
        const r = await fetch(`${API}/routes`);
        const d = await r.json();
        const sel = document.getElementById(selectId);
        sel.innerHTML = '<option value="">Select Route</option>' + d.routes.map(rt => `<option value="${rt.id}" data-fare="${rt.fare_min}">${rt.name} (${rt.from} → ${rt.to})</option>`).join('');
    } catch (e) { console.error(e); }
}

async function bookTicket() {
    if (!authToken) return toast('Please login first', 'error');
    const routeId = document.getElementById('ticket-route').value;
    const from = document.getElementById('ticket-from').value.trim();
    const to = document.getElementById('ticket-to').value.trim();
    if (!routeId || !from || !to) return toast('Fill all fields', 'error');
    const sel = document.getElementById('ticket-route');
    const opt = sel.options[sel.selectedIndex];
    const fare = parseFloat(opt.dataset.fare || 10);
    try {
        const r = await fetch(`${API}/tickets?token=${authToken}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ route_id: routeId, route_name: opt.text.split('(')[0].trim(), from_stop: from, to_stop: to, fare }) });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || 'Booking failed');
        toast('Ticket booked!', 'success');
        document.getElementById('book-ticket-form').classList.add('hidden');
        loadTickets();
    } catch (e) { toast(e.message, 'error'); }
}

async function loadTickets() {
    if (!authToken) return;
    const el = document.getElementById('tickets-list');
    try {
        const r = await fetch(`${API}/tickets?token=${authToken}`);
        const d = await r.json();
        if (!d.tickets.length) { el.innerHTML = '<p style="text-align:center;color:var(--text2);padding:20px">No tickets yet</p>'; return; }
        el.innerHTML = d.tickets.map(t => `
            <div class="ticket-card">
                <div style="display:flex;justify-content:space-between;align-items:center"><h4>${t.route_name || t.route_id}</h4><span class="badge badge-active">${t.status}</span></div>
                <p style="color:var(--text2);font-size:0.85rem;margin:8px 0">${t.from_stop} → ${t.to_stop}<br>Fare: ₹${t.fare} · ${new Date(t.booked_at).toLocaleDateString()}</p>
                <div class="qr-box">${generateQRSvg(t.qr_data)}</div>
                <p style="text-align:center;font-size:0.72rem;color:var(--text3)">${t.qr_data}</p>
            </div>`).join('');
    } catch (e) { el.innerHTML = '<p style="color:var(--danger)">Failed to load tickets</p>'; }
}

function generateQRSvg(data) {
    // Simple visual QR-like pattern based on string hash
    let hash = 0;
    for (let i = 0; i < data.length; i++) hash = ((hash << 5) - hash) + data.charCodeAt(i);
    const size = 9, cells = [];
    for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
        const bit = ((hash >> ((y * size + x) % 31)) & 1) || (x < 3 && y < 3) || (x >= size - 3 && y < 3) || (x < 3 && y >= size - 3);
        if (bit) cells.push(`<rect x="${x * 10 + 5}" y="${y * 10 + 5}" width="9" height="9" rx="1.5" fill="#0d9488"/>`);
    }
    return `<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">${cells.join('')}</svg>`;
}

// ── Saved Routes ─────────────────────────────────────────────────────
async function saveRoute() {
    if (!authToken) return toast('Please login first', 'error');
    const name = document.getElementById('save-name').value.trim();
    const from = document.getElementById('save-from').value.trim();
    const to = document.getElementById('save-to').value.trim();
    if (!from || !to) return toast('Enter from and to locations', 'error');
    try {
        const r = await fetch(`${API}/saved-routes?token=${authToken}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name || `${from} → ${to}`, from_place: from, to_place: to }) });
        if (!r.ok) throw new Error('Failed to save');
        toast('Route saved!', 'success');
        document.getElementById('save-name').value = ''; document.getElementById('save-from').value = ''; document.getElementById('save-to').value = '';
        loadSavedRoutes();
    } catch (e) { toast(e.message, 'error'); }
}

async function loadSavedRoutes() {
    if (!authToken) return;
    const el = document.getElementById('saved-list');
    try {
        const r = await fetch(`${API}/saved-routes?token=${authToken}`);
        const d = await r.json();
        if (!d.saved_routes.length) { el.innerHTML = '<p style="text-align:center;color:var(--text2);padding:20px">No saved routes</p>'; return; }
        el.innerHTML = d.saved_routes.map(sr => `
            <div class="result-item" style="display:flex;justify-content:space-between;align-items:center">
                <div><h4>${sr.name || 'Route'}</h4><p>${sr.from_place} → ${sr.to_place}</p></div>
                <button class="btn btn-danger btn-sm" onclick="deleteSavedRoute('${sr.id}')"><i class="fas fa-trash"></i></button>
            </div>`).join('');
    } catch (e) { el.innerHTML = '<p style="color:var(--danger)">Failed to load</p>'; }
}

async function deleteSavedRoute(id) {
    try {
        await fetch(`${API}/saved-routes/${id}?token=${authToken}`, { method: 'DELETE' });
        toast('Route removed', 'info');
        loadSavedRoutes();
    } catch (e) { toast('Failed to delete', 'error'); }
}

// ── Smart ETA (MVP) ──────────────────────────────────────────────────
let etaMap = null;

function detectMyLocation() {
    if (!navigator.geolocation) return toast('Geolocation not supported', 'error');
    const btn = document.getElementById('gps-detect-btn');
    const status = document.getElementById('gps-status');
    btn.disabled = true;
    status.textContent = 'Detecting location...';
    navigator.geolocation.getCurrentPosition(
        (pos) => {
            document.getElementById('eta-user-lat').value = pos.coords.latitude;
            document.getElementById('eta-user-lng').value = pos.coords.longitude;
            btn.innerHTML = '<i class="fas fa-check-circle"></i> Location Detected';
            btn.style.background = 'var(--success)';
            status.textContent = `📍 ${pos.coords.latitude.toFixed(4)}, ${pos.coords.longitude.toFixed(4)}`;
            toast('Location detected!', 'success');
        },
        () => {
            // Fallback to Kolkata center for demo
            document.getElementById('eta-user-lat').value = '22.5726';
            document.getElementById('eta-user-lng').value = '88.3639';
            btn.innerHTML = '<i class="fas fa-map-marker-alt"></i> Using Default (Kolkata)';
            btn.style.background = 'var(--warning)';
            status.textContent = 'GPS unavailable — using Kolkata center as default';
            btn.disabled = false;
            toast('GPS unavailable, using Kolkata center', 'info');
        },
        { enableHighAccuracy: true, timeout: 10000 }
    );
}

async function smartETA() {
    const lat = document.getElementById('eta-user-lat').value;
    const lng = document.getElementById('eta-user-lng').value;
    const dest = document.getElementById('eta-destination').value.trim();

    if (!lat || !lng) return toast('Please detect your location first', 'error');
    if (!dest) return toast('Please enter a destination', 'error');

    const el = document.getElementById('eta-results');
    el.innerHTML = '<p style="text-align:center;color:var(--text2);padding:16px"><i class="fas fa-spinner fa-spin"></i> Calculating ETA...</p>';

    try {
        const r = await fetch(`${API}/smart-eta?user_lat=${lat}&user_lng=${lng}&destination=${encodeURIComponent(dest)}`);
        if (!r.ok) {
            const err = await r.json();
            throw new Error(err.detail || 'Failed to calculate ETA');
        }
        const d = await r.json();

        // Determine traffic label from auto-detected data
        const ti = d.traffic_index;
        let trafficLabel, trafficColor;
        if (ti <= 0.3) { trafficLabel = '🟢 Light'; trafficColor = 'var(--success)'; }
        else if (ti <= 0.6) { trafficLabel = '🟡 Moderate'; trafficColor = 'var(--warning)'; }
        else if (ti <= 0.8) { trafficLabel = '🟠 Heavy'; trafficColor = 'var(--warning)'; }
        else { trafficLabel = '🔴 Very Heavy'; trafficColor = 'var(--danger)'; }
        const trafficSrc = d.traffic_source === 'live_speed' ? '📡 from live bus speed' : '🕐 estimated from time of day';

        // Build the big ETA display
        let html = '';

        // Total ETA hero card
        html += `<div class="result-item" style="border-left-color:var(--accent);text-align:center;padding:20px">
            <div style="font-size:2.6rem;font-weight:800;color:var(--accent)">${Math.round(d.eta.total_min)} min</div>
            <div style="font-size:0.9rem;color:var(--text2);margin-top:4px">Estimated Total Travel Time</div>
            <div style="font-size:0.75rem;color:var(--text3);margin-top:4px">Source: ${d.eta.source === 'ml_model' ? '🤖 AI/ML Model' : '📊 Formula'} · Traffic: <span style="color:${trafficColor}">${trafficLabel}</span> <span style="font-size:0.7rem">(${trafficSrc})</span></div>
        </div>`;

        // Breakdown card
        html += `<div class="result-item" style="border-left-color:var(--info)">
            <h4><i class="fas fa-list-ol" style="color:var(--info)"></i> ETA Breakdown</h4>
            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:10px">
                <div style="text-align:center;padding:10px;background:var(--bg3);border-radius:8px">
                    <div style="font-size:1.3rem;font-weight:700;color:var(--accent)">🚶 ${Math.round(d.eta.walk_time_min)}</div>
                    <div style="font-size:0.7rem;color:var(--text3)">Walk (min)</div>
                </div>
                <div style="text-align:center;padding:10px;background:var(--bg3);border-radius:8px">
                    <div style="font-size:1.3rem;font-weight:700;color:var(--warning)">⏳ ${Math.round(d.eta.wait_time_min)}</div>
                    <div style="font-size:0.7rem;color:var(--text3)">Wait (min)</div>
                </div>
                <div style="text-align:center;padding:10px;background:var(--bg3);border-radius:8px">
                    <div style="font-size:1.3rem;font-weight:700;color:var(--info)">🚌 ${Math.round(d.eta.bus_travel_min)}</div>
                    <div style="font-size:0.7rem;color:var(--text3)">Bus (min)</div>
                </div>
            </div>
        </div>`;

        // Route info card
        html += `<div class="result-item" style="border-left-color:var(--success)">
            <h4><i class="fas fa-route" style="color:var(--success)"></i> Your Route</h4>
            <div style="margin-top:8px;font-size:0.88rem">
                <p>📍 <b>Walk to:</b> ${d.pickup_stop.name} <span style="color:var(--text3)">(${d.pickup_stop.distance_km} km away)</span></p>
                <p>🚌 <b>Take Bus:</b> <span style="color:var(--accent);font-weight:700">${d.bus_route.name}</span> ${d.bus_route.via ? '(via ' + d.bus_route.via + ')' : ''}</p>
                ${d.transfer ? '<p>🔄 <b>Transfer at:</b> ' + d.transfer.stop.name + ' → Take <span style="color:var(--accent);font-weight:700">' + d.transfer.route.name + '</span></p>' : ''}
                <p>🏁 <b>Get off at:</b> ${d.destination_stop.name}</p>
                <p style="margin-top:6px">💰 <b>Fare:</b> ${d.bus_route.fare_range} · 📏 <b>Distance:</b> ${d.distance_km} km</p>
                <p style="color:var(--text3);font-size:0.8rem">Bus frequency: every ${d.bus_route.frequency_min} min</p>
            </div>
        </div>`;

        // Live bus info (if available)
        if (d.live_bus) {
            const crowdBadge = d.live_bus.crowd_level === 'Low' ? 'badge-low' : d.live_bus.crowd_level === 'Medium' ? 'badge-medium' : 'badge-high';
            html += `<div class="result-item" style="border-left-color:var(--success);background:rgba(16,185,129,0.05)">
                <h4 style="color:var(--success)"><i class="fas fa-broadcast-tower"></i> 🟢 Live Bus Spotted!</h4>
                <p style="margin-top:6px">Bus <b>${d.live_bus.bus_reg}</b> is ${d.live_bus.distance_km} km away from your stop</p>
                <p>Speed: ${d.live_bus.speed} km/h · Crowd: <span class="badge ${crowdBadge}">${d.live_bus.crowd_level}</span></p>
                <p style="color:var(--success);font-weight:700">Arrives at your stop in ~${Math.round(d.live_bus.live_eta_min)} min</p>
                <button class="btn btn-primary btn-sm" onclick="trackBus('${d.live_bus.bus_reg}')" style="margin-top:8px"><i class="fas fa-crosshairs"></i> Track This Bus Live</button>
            </div>`;
        } else {
            html += `<div class="result-item" style="border-left-color:var(--text3)">
                <p style="color:var(--text2);font-size:0.85rem"><i class="fas fa-info-circle"></i> No live bus currently on route ${d.bus_route.name}. ETA is based on schedule frequency (every ${d.bus_route.frequency_min} min).</p>
            </div>`;
        }

        el.innerHTML = html;
        toast('ETA calculated!', 'success');

        // Show mini map
        const mapEl = document.getElementById('eta-map');
        mapEl.style.display = 'block';
        if (!etaMap) {
            etaMap = L.map('eta-map').setView([d.pickup_stop.lat, d.pickup_stop.lng], 13);
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap' }).addTo(etaMap);
        } else {
            etaMap.eachLayer(l => { if (l instanceof L.Marker || l instanceof L.Polyline) etaMap.removeLayer(l); });
        }
        setTimeout(() => etaMap.invalidateSize(), 100);

        // User location marker
        L.marker([d.user_location.lat, d.user_location.lng], {
            icon: L.divIcon({ className: '', html: '<div style="background:#3b82f6;width:14px;height:14px;border-radius:50%;border:3px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,0.3)"></div>', iconSize: [14, 14] })
        }).addTo(etaMap).bindPopup('📍 You are here');

        // Pickup stop marker
        L.marker([d.pickup_stop.lat, d.pickup_stop.lng], {
            icon: L.divIcon({ className: '', html: '<div style="background:#10b981;width:16px;height:16px;border-radius:50%;border:3px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,0.3)"></div>', iconSize: [16, 16] })
        }).addTo(etaMap).bindPopup(`🚏 ${d.pickup_stop.name}`).openPopup();

        // Destination marker
        L.marker([d.destination_stop.lat, d.destination_stop.lng], {
            icon: L.divIcon({ className: '', html: '<div style="background:#ef4444;width:16px;height:16px;border-radius:4px;border:3px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,0.3)"></div>', iconSize: [16, 16] })
        }).addTo(etaMap).bindPopup(`🏁 ${d.destination_stop.name}`);

        // Draw line
        L.polyline([
            [d.user_location.lat, d.user_location.lng],
            [d.pickup_stop.lat, d.pickup_stop.lng],
            [d.destination_stop.lat, d.destination_stop.lng]
        ], { color: '#0d9488', weight: 3, dashArray: '8,4' }).addTo(etaMap);

        // Fit bounds
        etaMap.fitBounds([
            [d.user_location.lat, d.user_location.lng],
            [d.pickup_stop.lat, d.pickup_stop.lng],
            [d.destination_stop.lat, d.destination_stop.lng]
        ], { padding: [30, 30] });

    } catch (e) {
        el.innerHTML = `<div class="result-item" style="border-left-color:var(--danger)"><p style="color:var(--danger)"><i class="fas fa-exclamation-circle"></i> ${e.message}</p></div>`;
    }
}

// ── AI Chat ──────────────────────────────────────────────────────────
function initChat() {
    const box = document.getElementById('chat-box');
    if (box.children.length === 0) {
        chatHistory = [];
        appendChat('Hello! I\'m your SmartTransit assistant. Ask me anything about Kolkata\'s bus routes, stops, and transit!', 'ai');
    }
}

function appendChat(msg, sender) {
    const box = document.getElementById('chat-box');
    const d = document.createElement('div');
    d.className = `chat-msg chat-${sender}`;
    d.textContent = msg;
    box.appendChild(d);
    box.scrollTop = box.scrollHeight;
}

async function sendChat() {
    const input = document.getElementById('chat-input');
    const msg = input.value.trim();
    if (!msg) return;
    appendChat(msg, 'user');
    input.value = '';
    appendChat('Thinking...', 'thinking');
    chatHistory.push({ role: 'user', parts: [{ text: msg }] });
    try {
        const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key=${GEMINI_KEY}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                contents: chatHistory,
                systemInstruction: { role: 'system', parts: [{ text: 'You are SmartTransit AI, a helpful public transport assistant for Kolkata, India. Give concise answers about bus routes, stops, timings. Be friendly and helpful.' }] }
            })
        });
        const d = await r.json();
        document.querySelector('.chat-thinking')?.remove();
        if (d.candidates?.[0]) {
            const reply = d.candidates[0].content.parts[0].text;
            chatHistory.push({ role: 'model', parts: [{ text: reply }] });
            appendChat(reply, 'ai');
        } else { appendChat('Sorry, I couldn\'t process that request.', 'ai'); }
    } catch (e) { document.querySelector('.chat-thinking')?.remove(); appendChat('Error: ' + e.message, 'ai'); chatHistory.pop(); }
}

// ── Driver Dashboard ─────────────────────────────────────────────────
async function loadDriverDashboard() {
    const g = document.getElementById('d-greeting');
    if (currentUser) g.textContent = `Hello, ${currentUser.name}!`;
    loadRouteOptions('d-route');
}

function enableGPS() {
    if (!navigator.geolocation) return toast('GPS not supported', 'error');
    const btn = document.getElementById('gps-btn');
    locationWatchId = navigator.geolocation.watchPosition(onDriverPos, () => toast('GPS error', 'error'), { enableHighAccuracy: true, timeout: 10000 });
    btn.innerHTML = '<i class="fas fa-check-circle"></i> GPS Active';
    btn.style.background = 'var(--success)'; btn.disabled = true;
    toast('GPS enabled', 'success');
}

async function onDriverPos(pos) {
    const busReg = document.getElementById('d-busreg').value.trim().toUpperCase();
    if (!busReg) return;
    const { latitude, longitude, speed: rawSpeed } = pos.coords;
    const speed = rawSpeed ? (rawSpeed * 3.6).toFixed(1) : '0.0';
    const el = document.getElementById('live-speed');
    if (el) el.textContent = speed;
    if (tripCoords.length > 0) {
        const last = tripCoords[tripCoords.length - 1];
        tripDist += haversine(last[0], last[1], latitude, longitude);
        const distEl = document.getElementById('trip-dist');
        if (distEl) distEl.textContent = tripDist.toFixed(2);
    }
    tripCoords.push([latitude, longitude]);
    try {
        await fetch(`${API}/bus/update-location`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bus_reg: busReg, latitude, longitude, speed: parseFloat(speed), route_id: document.getElementById('d-route').value }) });
    } catch (e) { console.error(e); }
}

function haversine(lat1, lon1, lat2, lon2) {
    const R = 6371, dLat = (lat2 - lat1) * Math.PI / 180, dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

async function startTrip() {
    const busReg = document.getElementById('d-busreg').value.trim().toUpperCase();
    if (!busReg) return toast('Enter bus registration', 'error');
    if (!locationWatchId) return toast('Enable GPS first', 'error');
    tripDist = 0; tripCoords = []; tripSeconds = 0; paxCount = 0;
    try {
        await fetch(`${API}/bus/start-trip`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bus_reg: busReg, route_id: document.getElementById('d-route').value }) });
    } catch (e) { console.error(e); }
    document.getElementById('pre-trip').classList.add('hidden');
    document.getElementById('live-trip').classList.remove('hidden');
    tripTimer = setInterval(() => {
        tripSeconds++;
        const h = String(Math.floor(tripSeconds / 3600)).padStart(2, '0');
        const m = String(Math.floor((tripSeconds % 3600) / 60)).padStart(2, '0');
        const s = String(tripSeconds % 60).padStart(2, '0');
        const el = document.getElementById('trip-timer');
        if (el) el.textContent = `${h}:${m}:${s}`;
    }, 1000);
    toast('Trip started!', 'success');
}

function adjustCount(delta) { paxCount = Math.max(0, paxCount + delta); document.getElementById('pax-count').textContent = paxCount; }

async function updatePaxCount() {
    const busReg = document.getElementById('d-busreg').value.trim().toUpperCase();
    if (!busReg) return;
    try {
        const r = await fetch(`${API}/bus/update-passengers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bus_reg: busReg, passenger_count: paxCount }) });
        const d = await r.json();
        toast(`Count updated: ${d.crowd_level}`, 'success');
    } catch (e) { toast('Update failed', 'error'); }
}

async function reportStatus(status) {
    const busReg = document.getElementById('d-busreg').value.trim().toUpperCase();
    if (!busReg) return;
    let reason = '';
    if (status === 'delayed') reason = prompt('Reason for delay:') || 'Traffic';
    if (status === 'breakdown') reason = prompt('Breakdown details:') || 'Mechanical issue';
    try {
        await fetch(`${API}/bus/update-status`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bus_reg: busReg, status, delay_reason: reason }) });
        toast(`Status: ${status}`, status === 'running' ? 'success' : 'warning');
    } catch (e) { toast('Update failed', 'error'); }
}

async function endTrip() {
    if (locationWatchId) navigator.geolocation.clearWatch(locationWatchId);
    if (tripTimer) clearInterval(tripTimer);
    locationWatchId = null; tripTimer = null;
    const busReg = document.getElementById('d-busreg').value.trim().toUpperCase();
    if (busReg) try { await fetch(`${API}/bus/end-trip?bus_reg=${busReg}`, { method: 'POST' }); } catch (e) { console.error(e); }
    document.getElementById('sum-time').textContent = document.getElementById('trip-timer').textContent;
    document.getElementById('sum-dist').textContent = tripDist.toFixed(2);
    document.getElementById('live-trip').classList.add('hidden');
    document.getElementById('post-trip').classList.remove('hidden');
    toast('Trip ended!', 'info');
}

function resetTrip() {
    document.getElementById('post-trip').classList.add('hidden');
    document.getElementById('pre-trip').classList.remove('hidden');
    document.getElementById('d-busreg').value = '';
    const btn = document.getElementById('gps-btn');
    btn.innerHTML = '<i class="fas fa-satellite-dish"></i> Enable GPS';
    btn.style.background = ''; btn.disabled = false;
}

// ── Live Bus Tracking ────────────────────────────────────────────────
function initTrackMap() {
    if (!trackMap) {
        trackMap = L.map('track-map').setView([22.5726, 88.3639], 14);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap' }).addTo(trackMap);
    }
    setTimeout(() => trackMap.invalidateSize(), 100);
}

let _trackingBusReg = null;

async function trackBus(busReg) {
    _trackingBusReg = busReg;
    showPage('track-bus-page');
    const infoEl = document.getElementById('track-bus-info');
    infoEl.innerHTML = `<p style="text-align:center;color:var(--text2)"><i class="fas fa-spinner fa-spin"></i> Loading bus ${busReg}...</p>`;
    // Clear previous tracking
    if (trackInterval) clearInterval(trackInterval);
    if (trackMarker && trackMap) { trackMap.removeLayer(trackMarker); trackMarker = null; }
    // Fetch immediately, then every 5 seconds
    await updateTrackingView(busReg);
    trackInterval = setInterval(() => updateTrackingView(busReg), 5000);
}

async function updateTrackingView(busReg) {
    const infoEl = document.getElementById('track-bus-info');
    try {
        const r = await fetch(`${API}/bus/${busReg}`);
        if (!r.ok) {
            infoEl.innerHTML = `<p style="color:var(--danger);text-align:center"><i class="fas fa-exclamation-circle"></i> Bus ${busReg} is no longer active. Trip may have ended.</p>`;
            if (trackInterval) { clearInterval(trackInterval); trackInterval = null; }
            return;
        }
        const b = await r.json();
        const crowdBadge = b.crowd_level === 'Low' ? 'badge-low' : b.crowd_level === 'Medium' ? 'badge-medium' : 'badge-high';
        const statusColor = b.status === 'running' ? 'var(--success)' : b.status === 'delayed' ? 'var(--warning)' : 'var(--danger)';
        const statusIcon = b.status === 'running' ? 'fa-check-circle' : b.status === 'delayed' ? 'fa-exclamation-triangle' : 'fa-tools';
        // Find route info
        let routeInfo = '';
        try {
            const rr = await fetch(`${API}/routes/${b.route_id}`);
            if (rr.ok) { const rd = await rr.json(); routeInfo = `${rd.route.from} → ${rd.route.to}`; }
        } catch (e) { }

        infoEl.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                <h3 style="margin:0"><i class="fas fa-bus" style="color:var(--accent);margin-right:8px"></i>${b.bus_reg}</h3>
                <span class="badge" style="background:${statusColor};color:#fff;padding:4px 12px;border-radius:20px"><i class="fas ${statusIcon}"></i> ${b.status.toUpperCase()}</span>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
                <div class="result-item" style="text-align:center;padding:12px">
                    <div style="font-size:1.5rem;font-weight:700;color:var(--accent)">${b.speed}</div>
                    <div style="font-size:0.75rem;color:var(--text2)">km/h</div>
                </div>
                <div class="result-item" style="text-align:center;padding:12px">
                    <div style="font-size:1.2rem;font-weight:700"><span class="badge ${crowdBadge}">${b.crowd_level}</span></div>
                    <div style="font-size:0.75rem;color:var(--text2)">${b.passenger_count} passengers</div>
                </div>
            </div>
            <p style="margin:8px 0;color:var(--text2);font-size:0.85rem"><i class="fas fa-route" style="color:var(--accent)"></i> Route: <b>${b.route_id || 'N/A'}</b> ${routeInfo ? '(' + routeInfo + ')' : ''}</p>
            ${b.delay_reason ? '<p style="color:var(--warning);font-size:0.85rem"><i class="fas fa-exclamation-triangle"></i> ' + b.delay_reason + '</p>' : ''}
            <p style="font-size:0.72rem;color:var(--text3);text-align:right"><i class="fas fa-sync-alt"></i> Auto-refreshing every 5s</p>`;

        // Update map marker
        const ll = [b.latitude, b.longitude];
        if (trackMarker) {
            trackMarker.setLatLng(ll);
        } else {
            const icon = L.icon({ iconUrl: 'https://img.icons8.com/plasticine/100/bus.png', iconSize: [52, 52] });
            trackMarker = L.marker(ll, { icon }).addTo(trackMap);
        }
        trackMap.setView(ll, 15, { animate: true });
        trackMarker.bindPopup(`<b>${b.bus_reg}</b><br>${b.route_id} · ${b.speed} km/h`).openPopup();
    } catch (e) {
        infoEl.innerHTML = `<p style="color:var(--danger)">Failed to load bus data: ${e.message}</p>`;
    }
}

function stopTracking() {
    if (trackInterval) { clearInterval(trackInterval); trackInterval = null; }
    if (trackMarker && trackMap) { trackMap.removeLayer(trackMarker); trackMarker = null; }
    _trackingBusReg = null;
    showPage('live-map-page');
}

// ── Init ─────────────────────────────────────────────────────────────
showPage('landing-page');
