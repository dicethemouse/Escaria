---
layout: default
title: Aulonia
---

<section class="home-hero">

# Aulonia

Eine interaktive Enzyklopädie und Karte der Welt Aulonia.

</section>

<section class="wiki-overview">

## Die Welt erkunden

<div class="collection-grid">

  <a class="collection-card" href="{{ '/states/' | relative_url }}">
    <strong>Staaten</strong>
    <span>{{ site.states | size }}</span>
  </a>

  <a class="collection-card" href="{{ '/provinces/' | relative_url }}">
    <strong>Provinzen</strong>
    <span>{{ site.provinces | size }}</span>
  </a>

  <a class="collection-card" href="{{ '/settlements/' | relative_url }}">
    <strong>Siedlungen</strong>
    <span>{{ site.settlements | size }}</span>
  </a>

  <a class="collection-card" href="{{ '/markets/' | relative_url }}">
    <strong>Märkte</strong>
    <span>{{ site.markets | size }}</span>
  </a>

  <a class="collection-card" href="{{ '/rivers/' | relative_url }}">
    <strong>Flüsse</strong>
    <span>{{ site.rivers | size }}</span>
  </a>

  <a class="collection-card" href="{{ '/lakes/' | relative_url }}">
    <strong>Seen</strong>
    <span>{{ site.lakes | size }}</span>
  </a>

  <a class="collection-card" href="{{ '/routes/' | relative_url }}">
    <strong>Routen</strong>
    <span>{{ site.routes | size }}</span>
  </a>

  <a class="collection-card" href="{{ '/pois/' | relative_url }}">
    <strong>Orte</strong>
    <span>{{ site.pois | size }}</span>
  </a>

</div>
</section>