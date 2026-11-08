# avant-vigia
Avant Figueres Vilafant ⇄ Barcelona Sants: web con plazas libres/completas y aviso push (ntfy) cuando se libera un tren completo.
Todo corre en GitHub Actions cada ~10 min; la web es GitHub Pages (rama gh-pages, la genera el workflow).
El único secreto es el topic de ntfy (secret NTFY_TOPIC): no está en el código ni en la web.
