// static/js/main.js
let map;
let markers = [];
let polylines = [];
let meMarker = null;
let targetMarker = null;
let eventSource = null;
let lastLatLng = null;
let socket = null;

let traceEvents = [];
let replayBtn = null;
let isReplaying = false;
let currentPlayer = PLAYER;
let currentChallenge = CHALLENGE;

const REPLAY_SPEED_MS = 1200; // hop-by-hop playback speed (ms)

// track the last hop's marker/latlng so we can mark destination red on "end"
let lastHopMarker = null;
let lastHopLatLng = null;
let destHighlight = null;

// --- helpers -------------------------------------------------

function clearMap() {
  markers.forEach(m => m.remove());
  markers = [];
  polylines.forEach(p => p.remove());
  polylines = [];
  lastLatLng = null;

  if (meMarker) { meMarker.remove(); meMarker = null; }
  if (targetMarker) { targetMarker.remove(); targetMarker = null; }
  if (destHighlight) { destHighlight.remove(); destHighlight = null; }
  lastHopMarker = null;
  lastHopLatLng = null;
}

function log(msg) {
  const el = document.getElementById('log');
  const time = new Date().toLocaleTimeString();
  el.innerHTML += `[${time}] ${msg}<br/>`;
  el.scrollTop = el.scrollHeight;
}

function setStatus(s) {
  document.getElementById('status').textContent = s;
}

// tiny random offset for overlapping hops; never used for no-geo hops
function jitterLatLng(lat, lon) {
  const J = 0.1; // ~11km
  return [lat + J * (Math.random() - 0.5), lon + J * (Math.random() - 0.5)];
}

// --- map features --------------------------------------------

function addHopMarker(hop, ip, geo, {
  jitter = true,
  logLine = true,
  latlngOverride = null // use exact coords (e.g., saved jitter) if provided
} = {}) {
  if (!geo || typeof geo.lat !== 'number' || typeof geo.lon !== 'number') {
    if (logLine) log(`Hop ${hop}: (no response)`);
    return;
  }

  let latlng = latlngOverride && Array.isArray(latlngOverride)
    ? latlngOverride
    : (jitter ? jitterLatLng(geo.lat, geo.lon) : [geo.lat, geo.lon]);

  const marker = L.marker(latlng).addTo(map);
  marker.bindTooltip(
    `#${hop} ${ip}<br/>${geo.city || ''} ${geo.country || ''}<br/>${geo.org || ''}`,
    { permanent: false, direction: 'top', className: 'my-tooltip' }
  );
  markers.push(marker);

  if (lastLatLng) {
    const line = L.polyline([lastLatLng, latlng], { weight: 3 }).addTo(map);
    polylines.push(line);
  }
  lastLatLng = latlng;
  lastHopMarker = marker;
  lastHopLatLng = latlng;

  map.panTo(latlng, { animate: true });

  if (logLine) log(`Hop ${hop}: ${ip} (${geo.city || ''} ${geo.country || ''})`);
}

function placeMe(me, { seedLine = false, zoomOnMe = false } = {}) {
  if (!me || !me.lat || !me.lon) return;

  if (meMarker) { meMarker.remove(); meMarker = null; }

  const icon = L.divIcon({ className: 'pulse' });
  meMarker = L.marker([me.lat, me.lon], { icon, zIndexOffset: 1000 }).addTo(map);
  meMarker.bindTooltip(
    `You: ${me.query || me.ip} ${me.city ? '('+me.city+', '+me.country+')' : ''}`,
    { direction: 'top', className: 'my-tooltip' }
  );

  if (zoomOnMe) {
    map.setView([me.lat, me.lon], 7); // << zoom in on start
  } else {
    map.setView([me.lat, me.lon], 3);
  }

  if (seedLine) {
    lastLatLng = [me.lat, me.lon];
  }
}

function placeTarget(challenge) {
  // Don't show the target location or circle - keep it secret!
  // Players need to figure out where to trace to get close to the mystery destination
  // Target area hidden from players - mystery mode
}

function highlightDestination(latlng) {
  if (!latlng) return;
  if (destHighlight) { destHighlight.remove(); destHighlight = null; }

  // red circle overlay to mark final hop
  destHighlight = L.circleMarker(latlng, {
    radius: 10,
    weight: 3,
    color: 'red',
    fillColor: 'red',
    fillOpacity: 0.5
  }).addTo(map).bringToFront();
}

// --- player registration -------------------------------------

function showPlayerForm() {
  const section = document.getElementById('player-section');
  section.innerHTML = `
    <div class="player-register">
      <input id="player-name" type="text" placeholder="Enter your display name" maxlength="30" />
      <button id="register-btn" type="button">Join Race</button>
    </div>
  `;
  
  document.getElementById('register-btn').addEventListener('click', registerPlayer);
  document.getElementById('player-name').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') registerPlayer();
  });
}

function showPlayerInfo(player) {
  const section = document.getElementById('player-section');
  section.innerHTML = `
    <div class="player-info">
      <span>Player: <strong>${player.display_name}</strong></span>
      <button id="change-name-btn" type="button">Change Name</button>
    </div>
  `;
  
  document.getElementById('change-name-btn').addEventListener('click', showPlayerForm);
}

async function registerPlayer() {
  console.log('registerPlayer() function called');
  const nameInput = document.getElementById('player-name');
  const name = nameInput.value.trim();
  
  console.log('Player name entered:', name);
  
  if (!name) {
    console.log('No name entered');
    alert('Please enter a display name');
    return;
  }
  
  console.log('Sending registration request to /register');
  try {
    const response = await fetch('/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: name })
    });
    
    console.log('Registration response status:', response.status);
    if (response.ok) {
      const data = await response.json();
      console.log('Registration successful, data:', data);
      currentPlayer = data.player;
      showPlayerInfo(currentPlayer);
      log(`Registered as: ${currentPlayer.display_name}`);
      
      // Auto-refresh scoreboard to show the new player
      if (document.getElementById('scoreboard-modal').style.display === 'block') {
        showScoreboard();
      }
    } else {
      const errorText = await response.text();
      console.log('Registration failed:', response.status, errorText);
      alert('Failed to register. Please try again.');
    }
  } catch (error) {
    console.error('Registration error:', error);
    alert('Failed to register. Please try again.');
  }
}

// --- challenge countdown -------------------------------------

function updateCountdown() {
  if (!currentChallenge || !currentChallenge.end_time) return;
  
  const now = new Date();
  const end = new Date(currentChallenge.end_time);
  const diff = end - now;
  
  if (diff <= 0) {
    document.getElementById('countdown').textContent = 'Challenge ended';
    return;
  }
  
  const hours = Math.floor(diff / (1000 * 60 * 60));
  const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
  const seconds = Math.floor((diff % (1000 * 60)) / 1000);
  
  document.getElementById('countdown').textContent = 
    `Time remaining: ${hours}h ${minutes}m ${seconds}s`;
}

// --- scoreboard ----------------------------------------------

async function showScoreboard() {
  const modal = document.getElementById('scoreboard-modal');
  const content = document.getElementById('scoreboard-content');
  
  modal.style.display = 'block';
  content.innerHTML = 'Loading...';
  
  try {
    const response = await fetch('/scoreboard');
    const data = await response.json();
    
    if (data.results && data.results.length > 0) {
      let html = '<div class="scoreboard-list">';
      
      data.results.forEach((result, index) => {
        const isWaiting = result.status === 'waiting';
        const rankClass = !isWaiting && index === 0 ? 'gold' : !isWaiting && index === 1 ? 'silver' : !isWaiting && index === 2 ? 'bronze' : '';
        const duration = Math.round(result.trace_duration_seconds);
        
        let rankDisplay, infoDisplay, pointsDisplay;
        
        if (isWaiting) {
          rankDisplay = '-';
          if (result.distance_from_target_km > 0) {
            // Player attempted but didn't win
            infoDisplay = `Attempted: ${result.distance_from_target_km.toFixed(0)}km away • ${result.total_hops} hops`;
          } else {
            // Player just joined, hasn't attempted yet
            infoDisplay = 'Waiting to start race...';
          }
          pointsDisplay = '0 pts';
        } else {
          rankDisplay = `#${result.rank}`;
          infoDisplay = `${result.total_hops} hops • ${duration}s • Mystery solved!`;
          pointsDisplay = `${result.points} pts`;
        }
        
        html += `
          <div class="score-entry ${isWaiting ? 'waiting' : ''}">
            <div class="score-rank ${rankClass}">${rankDisplay}</div>
            <div class="score-details">
              <div class="score-name">${result.player_name}</div>
              <div class="score-info">${infoDisplay}</div>
            </div>
            <div class="score-points">${pointsDisplay}</div>
          </div>
        `;
      });
      
      html += '</div>';
      content.innerHTML = html;
    } else {
      content.innerHTML = '<p>No players have joined yet. Be the first to join the race!</p>';
    }
  } catch (error) {
    console.error('Failed to load scoreboard:', error);
    content.innerHTML = '<p>Failed to load scoreboard. Please try again.</p>';
  }
}

async function downloadResults() {
  window.location.href = '/download';
}

// --- replay ---------------------------------------------------

function enableReplayButton(enable) {
  if (!replayBtn) return;
  replayBtn.disabled = !enable;
  replayBtn.textContent = enable ? 'Replay Trace' : 'Replay Trace (waiting)';
}

async function replayTrace(speedMs = REPLAY_SPEED_MS) {
  if (!traceEvents.length || isReplaying) return;
  isReplaying = true;
  setStatus('Replaying...');
  log('Replay: starting...');

  clearMap();
  if (ME && ME.lat && ME.lon) {
    placeMe(ME, { seedLine: true, zoomOnMe: true });
  } else {
    map.setView([20, 0], 2);
    lastLatLng = null;
  }
  
  if (currentChallenge) {
    placeTarget(currentChallenge);
  }

  for (const evt of traceEvents) {
    if (evt.type === 'hop') {
      const { hop, ip, geo, geo_jittered } = evt;
      if (ip && geo) {
        const override = (geo_jittered && typeof geo_jittered.lat === 'number' && typeof geo_jittered.lon === 'number')
          ? [geo_jittered.lat, geo_jittered.lon]
          : null;
        addHopMarker(hop, ip, geo, { jitter: !override, logLine: false, latlngOverride: override });
        log(`[Replay] Hop ${hop}: ${ip} (${geo.city || ''} ${geo.country || ''})`);
      } else if (ip && !geo) {
        log(`[Replay] Hop ${hop}: ${ip} (no geo)`);
      } else {
        log(`[Replay] Hop ${hop}: *`);
      }
      await new Promise(r => setTimeout(r, speedMs));
    } else if (evt.type === 'end') {
      // mark destination red on replay end as well
      highlightDestination(lastHopLatLng);
    }
  }

  setStatus('Replay done');
  log('Replay: complete.');
  isReplaying = false;
}

// --- websocket -----------------------------------------------

function initWebSocket() {
  socket = io();
  
  socket.on('connect', () => {
    console.log('Connected to server');
  });
  
  socket.on('new_challenge', (challenge) => {
    currentChallenge = challenge;
    location.reload(); // Simple reload to update UI
  });
  
  socket.on('race_finished', (data) => {
    const { result, distance_km } = data;
    log(`${result.player_name} finished in position #${result.rank} (${result.points} points)`);
    
    // If it's the current player, show celebration
    if (currentPlayer && result.player_id === currentPlayer.id) {
      const successDiv = document.createElement('div');
      successDiv.className = 'race-success';
      successDiv.innerHTML = `
        Congratulations! You finished in position #${result.rank}!<br/>
        You earned ${result.points} points!
      `;
      document.getElementById('log').prepend(successDiv);
    }
  });
  
  socket.on('player_joined', (data) => {
    const { player, result } = data;
    log(`${player.display_name} joined the race!`);
    // Scoreboard will automatically update on next view since it pulls from server
  });
  
  socket.on('challenge_mode_changed', (data) => {
    const { enabled } = data;
    if (enabled !== !!currentChallenge) {
      location.reload(); // Simple reload to update UI
    }
  });
}

// --- init & events -------------------------------------------

function init() {
  map = L.map('map', { worldCopyJump: true }).setView([20, 0], 2);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);

  if (ME && ME.lat && ME.lon) {
    placeMe(ME); // initial load: coarse zoom
  } else {
    map.setView([20, 0], 2);
  }
  
  // Place target marker if challenge exists
  if (currentChallenge) {
    placeTarget(currentChallenge);
  }

  // add Replay button
  const form = document.getElementById('trace-form');
  replayBtn = document.createElement('button');
  replayBtn.type = 'button';
  replayBtn.style.marginLeft = '6px';
  replayBtn.textContent = 'Replay Trace (waiting)';
  replayBtn.disabled = true;
  replayBtn.addEventListener('click', () => replayTrace(REPLAY_SPEED_MS));
  form.appendChild(replayBtn);

  // Player registration
  console.log('Player registration setup - currentPlayer:', currentPlayer);
  
  if (!currentPlayer || !currentPlayer.id) {
    console.log('Setting up registration for new player');
    const registerBtn = document.getElementById('register-btn');
    if (registerBtn) {
      console.log('Found register button, adding event listener');
      registerBtn.addEventListener('click', registerPlayer);
      const nameInput = document.getElementById('player-name');
      if (nameInput) {
        nameInput.addEventListener('keypress', (e) => {
          if (e.key === 'Enter') registerPlayer();
        });
      }
    } else {
      console.log('Register button not found');
    }
  } else {
    console.log('Setting up for existing player');
    const changeBtn = document.getElementById('change-name-btn');
    if (changeBtn) {
      changeBtn.addEventListener('click', showPlayerForm);
    }
  }

  // Scoreboard
  document.getElementById('scoreboard-btn').addEventListener('click', showScoreboard);
  document.getElementById('download-btn').addEventListener('click', downloadResults);
  
  // Modal close
  document.querySelector('.close').addEventListener('click', () => {
    document.getElementById('scoreboard-modal').style.display = 'none';
  });
  
  window.addEventListener('click', (event) => {
    const modal = document.getElementById('scoreboard-modal');
    if (event.target === modal) {
      modal.style.display = 'none';
    }
  });

  // Countdown
  if (currentChallenge) {
    updateCountdown();
    setInterval(updateCountdown, 1000);
  }

  // Challenge toggle button
  const toggleBtn = document.getElementById('challenge-toggle-btn');
  if (toggleBtn) {
    toggleBtn.addEventListener('click', async () => {
      const challengeSection = document.getElementById('challenge-section');
      const isVisible = challengeSection.style.display !== 'none';
      
      if (isVisible) {
        // Just hide the UI
        challengeSection.style.display = 'none';
        toggleBtn.textContent = 'Show Challenge Mode';
      } else {
        // Show the UI
        challengeSection.style.display = 'block';
        toggleBtn.textContent = 'Hide Challenge Mode';
      }
    });
  }

  // Main trace form
  document.getElementById('trace-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const target = document.getElementById('target').value.trim();
    if (!target) return;

    // Check if player is registered for races
    if (currentChallenge && target === currentChallenge.target_host && !currentPlayer) {
      alert('Please register with a display name before starting a race!');
      return;
    }

    if (eventSource) { eventSource.close(); eventSource = null; }

    clearMap();
    traceEvents = [];
    enableReplayButton(false);

    // place "me" and zoom in when starting a new trace; also seed polyline
    if (ME && ME.lat && ME.lon) {
      placeMe(ME, { seedLine: true, zoomOnMe: true });
    }
    
    // Place target if this is a race
    if (currentChallenge && target === currentChallenge.target_host) {
      placeTarget(currentChallenge);
    }

    setStatus('Tracing...');
    log(`Starting traceroute to ${target} ...`);

    eventSource = new EventSource(`/stream?target=${encodeURIComponent(target)}`);

    eventSource.onmessage = (ev) => {
      const data = JSON.parse(ev.data);

      if (data.type === 'start') {
        traceEvents.push(data);
        if (data.is_race) {
          log('Race started! Good luck!');
        }
        return;
      }

      if (data.type === 'hop') {
        // live: compute jitter once, store it, never jitter no-geo
        if (data.ip && data.geo) {
          const jittered = jitterLatLng(data.geo.lat, data.geo.lon);
          addHopMarker(data.hop, data.ip, data.geo, { jitter: false, latlngOverride: jittered });
          data.geo_jittered = { lat: jittered[0], lon: jittered[1] };
          traceEvents.push(data);
        } else if (data.ip) {
          fetch(`/geo/${data.ip}`)
            .then(r => r.json())
            .then(res => {
              if (res.ok) {
                const jittered = jitterLatLng(res.geo.lat, res.geo.lon);
                addHopMarker(data.hop, data.ip, res.geo, { jitter: false, latlngOverride: jittered });
                traceEvents.push({ ...data, geo: res.geo, geo_jittered: { lat: jittered[0], lon: jittered[1] } });
                log(`Hop ${data.hop}: ${data.ip} (${res.geo.city || ''} ${res.geo.country || ''})`);
              } else {
                log(`Hop ${data.hop}: ${data.ip} (no geo)`);
                traceEvents.push(data);
              }
            })
            .catch(() => { log(`Hop ${data.hop}: ${data.ip}`); traceEvents.push(data); });
        } else {
          log(`Hop ${data.hop}: *`);
          traceEvents.push(data);
        }
        return;
      }

      if (data.type === 'race_success') {
        const successDiv = document.createElement('div');
        successDiv.className = 'race-success';
        successDiv.innerHTML = `
          SUCCESS! You reached the mystery location!<br/>
          Position: #${data.rank} • Points: ${data.points} • Distance: ${data.distance_km}km
        `;
        document.getElementById('log').prepend(successDiv);
        setStatus('Race completed!');
        
        // Show success alert
        alert(`MYSTERY SOLVED!\n\nYou reached the mystery location!\n\nFinal location: ${data.final_location || 'Unknown'}\nDistance from target: ${data.distance_km}km\nRank: #${data.rank}\nPoints earned: ${data.points}`);
      }

      if (data.type === 'race_failed') {
        const failDiv = document.createElement('div');
        failDiv.className = 'race-failed';
        failDiv.innerHTML = `
          Keep searching! You're ${data.distance_km}km away<br/>
          Final hop: ${data.final_location}
        `;
        document.getElementById('log').prepend(failDiv);
        setStatus('Race failed');
        
        // Show distance feedback alert
        alert(`MYSTERY LOCATION FEEDBACK\n\nYour trace ended at: ${data.final_location}\nDistance from mystery location: ${data.distance_km}km\nNeeded: Within ${data.required_radius_km}km\n\nTry a different target to get closer!`);
      }

      if (data.type === 'distance_feedback') {
        // Show feedback for registered players only
        alert(`DISTANCE FEEDBACK\n\nYour trace ended at: ${data.final_location}\nDistance from mystery location: ${data.distance_km}km\n\nTry a different target to get closer to the mystery location!`);
      }

      if (data.type === 'end') {
        traceEvents.push(data);
        setStatus('Done');
        log('Trace complete.');

        // mark destination (final hop) in red for the live run
        highlightDestination(lastHopLatLng);

        if (eventSource) { eventSource.close(); eventSource = null; }
        enableReplayButton(traceEvents.some(e => e.type === 'hop'));
      }
    };

    eventSource.onerror = () => {
      setStatus('Error');
      log('Error: could not stream results (is traceroute installed and allowed by firewall?)');
      eventSource.close();
      eventSource = null;
      enableReplayButton(traceEvents.some(e => e.type === 'hop'));
    };
  });

  // Initialize WebSocket
  initWebSocket();
}

window.addEventListener('DOMContentLoaded', init);