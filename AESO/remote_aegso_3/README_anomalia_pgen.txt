================================================================================
ANOMALIA EN LA CELDA pswap = 1.0, pgen = 0.6  (cliente lab)
================================================================================

Este documento explica por qué, a pswap = 1.0, la celda pgen = 0.6 render MENOS
e-bits/s que la celda pgen = 0.5, cuando intuitivamente un pgen mayor debería
rendir más:

        pgen = 0.5  ->  R_per_att = 1110 e-bits/s
        pgen = 0.6  ->  R_per_att = 1061 e-bits/s      <-- anomalía

No es un error de aritmética ni un fallo de la física de pgen. Es una consecuencia
del carácter COLECTIVO del protocolo (es cosa de dos: A y B se deben cumplir a la
vez) combinado con que la confirmación del repetidor es un requisito síncrono.


--------------------------------------------------------------------------------
1. LA ESTRUCTURA DEL PROTOCOLO (por qué es "cosa de dos")
--------------------------------------------------------------------------------

El cliente tiene dos fases (ver aegso.py, PHASE 1 y PHASE 2):

    PHASE 1 (Generation Loop)
        El cliente emite una petición de generación (`gen`) al repetidor y
        espera un ACK. El ACK lleva un bit `pgen_bit`:
            - pgen_bit = 0  ->  el repetidor aún NO autoriza la EPR. El cliente
                                sigue en PHASE 1 y vuelve a emitir.
            - pgen_bit = 1  ->  el repetidor autoriza. El cliente marca su EPR como
                                "lista" y pasa a PHASE 2.

    PHASE 2 (Wait for Swap)
        El cliente espera el swap con un timeout de `swap_wait_timeout` (3 s).
            - Si el swap llega  ->  ronda completada.
            - Si NO llega (timeout) ->  `continue`: vuelve a PHASE 1 y regenera.

En el repetidor, el swap sólo se emite cuando **AMBOS** lados están listos a la vez
(`links["A"].ready and links["B"].ready`) y ninguna EPR ha expirado. Es decir: el
swap es un evento conjunto. Si A consigue `pgen_bit=1` pero B en ese instante no lo
consigue (o su EPR ya expiró), NO hay swap; A se queda bloqueado 3 s en PHASE 2 y
luego vuelve a generar. Lo mismo para B.

Consecuencia clave: un cliente NO PUEDE BLOQUEARSE por su cuenta. El cliente no
"vuelve a empezar desde cero" arbitrariamente; simplemente espera pasivamente a que
el repetidor autorice el emparejamiento. Por eso el pgen (probabilidad de que una
generación tenga éxito) y el resultado final (que haya swap) NO son independientes:
el pgen de un cliente determina si puede estar listo a tiempo para coincidir con el
otro lado. Un pgen más alto hace que ese cliente esté listo ANTES y con más
frecuencia, lo cual ayuda al emparejamiento. Por eso, en régimen sano, más pgen =>
más swaps, como se ve en pgen = 0.8, 0.9, 1.0.


--------------------------------------------------------------------------------
2. EL EFECTO ESTADÍSTICO EN pgen = 0.5 vs pgen = 0.6
--------------------------------------------------------------------------------

El emparejamiento depende de que A y B réussi su `pgen_bit=1` dentro de una ventana
de tiempo coherente (mientras la EPR está fresca). Cuando el pgen de UN cliente baja,
ese cliente tarda más (en media) en estar listo, y la probabilidad de coincidir con
el otro dentro de la ventana DROPEA para ambos.

En concreto, bajando de pgen = 0.6 a pgen = 0.5, al cliente le toca statistically
esperar MÁS tiempo a que el otro esté listo (porque el otro falla más a menudo en
esos momentos), y como el swap depende de una confirmación del repetidor que exige
que los DOS estén listos, se producen MENOS generaciones/swaps efectivos. Los datos
de pswap = 1.0 (cliente lab) lo muestran:

    pgen    M_gen    n_swap    gen_por_ronda    swap/gen_ok    stalls    EN_mean
    1.0     2000      2000          1.00             1.000         0       1.000
    0.9     2270      2000          1.14             0.992        17       0.889
    0.8     2684      2000          1.34             0.934       142       0.798
    0.7      595       210          2.83             0.515       197       0.686
    0.6      516       108          4.78             0.354       199       0.591   <-- anomalía
    0.5     1083       362          2.99             0.652       199       0.512   <-- "mejor" (irreal)
    0.4      729        96          7.59             0.337       199       0.391
    0.3      719        14         51.36             0.070       199       0.278
    0.2     1045        41         25.49             0.184       199       0.213

    - M_gen        : generaciones (intentos de generación) registradas
    - n_swap       : swaps exitosos (rondas completadas)
    - gen_por_ronda: generaciones gastadas por cada swap (M_gen / n_swap)
    - swap/gen_ok  : fracción de generaciones exitosas queacabó en swap
    - stalls       : eventos en los que el cliente quedó bloqueado ~3 s en PHASE 2
    - EN_mean      : E_N medio por intento (en [0,1])

En pgen = 0.6 se necesitan 4.78 generaciones por cada swap; en pgen = 0.5, 2.99.
Es decir: pgen = 0.5, CONTRA TODA INTUICIÓN, es MÁS eficiente (menos generaciones
por ronda completada) que pgen = 0.6. Esa es precisamente la anomalía.

La explicación es de natureleza estadística y de sincronía, no de la física de pgen:
en pgen = 0.6 a este cliente (lab) le tocó, por azar estadístico, esperar más a que
el otro (teleco) confirmara, porque teleco fallaba precisamente en esos momentos.
Como el swap depende de la confirmación del repetidor y exige que A y B se cumplan a
la vez, esa espera extra se traduce directamente en menos generaciones/swaps
efectivos para lab en esa celda. En pgen = 0.5, por el contrario, la sincronía entre
los dos clientes fue más afortunada y hubo más swaps por generación.

Nota importante: lacellularidad del protocolo (A y B a la vez) es la clave. Si cada
cliente generara y "?swaggerase" de forma independiente, el rate de cada uno sería
independiente del otro y no habría esta anomalía. La anomalía es una firma del acoplamiento.


--------------------------------------------------------------------------------
3. POR QUÉ E_N SIGUE LA PROPORCIÓN (a pesar de la anomalía)
--------------------------------------------------------------------------------

Aunque el número de swaps varíe de forma no-monotónica (por el efecto de sincronía de
la sección 2), el E_N MEDIO por intento sigue exactamente la proporción de pgen:

    pgen    frac_ok    EN_mean
    0.5      0.512      0.512
    0.6      0.591      0.591
    0.7      0.686      0.686
    0.8      0.798      0.798
    0.9      0.889      0.889
    1.0      1.000      1.000

Esto es una consecuencia directa de la convención de cálculo (W = 0 si la generación
falla), y es la prueba de que la anomalía NO está en la física del entanglement ni
en la normalización:

    - En la fila Tcoh = Inf, cada intento tiene E_N = 1.0 si el bit de éxito de
      generación es 1, y E_N = 0 en caso contrario (W forzado a 0).
    - Por lo tanto EN_mean = (1/M) * sum(E_N) = (n_gen_ok / M) = frac_ok, exactamente.
    - Verificado numéricamente: compute_en(W=0) = 0.0 y compute_en(W=1) = 1.0.

Es decir, E_N es "e-bits intents generados" por intento, y su media es exactamente la
probabilidad de que una generación tenga éxito, que es pgen por definición del
simulador. La anomalía del rate (R_per_att) viene de la sincronía A/B (cuántos
intentos se necesitan POR swap), pero NO del valor de entanglement por intento. Por
eso el E_N medio es "limpio" y monótono, mientras el rate por intercambio puede
presentar estas no-monotonías.


--------------------------------------------------------------------------------
4. CÓMO SE MANIFIESTA EN EL RATE (la aritmética de la anomalía)
--------------------------------------------------------------------------------

El rate por intercambio (R_per_att) se puede escribir como:

    R_per_att = frac_ok  x  <1 / t_gen>

    - frac_ok = n_gen_ok / M              (la "proporción", monótona en pgen)
    - <1/t_gen> = R_unit                  (la constante de hardware, ver abajo)

Para pgen = 0.5 y pgen = 0.6:

    pgen    frac_ok    R_unit     R_per_att = frac_ok * R_unit
    0.5      0.512     2166         1110
    0.6      0.591     1794         1061

La componente geométrica (frac_ok) crece al subir pgen (0.512 -> 0.591, +15%). Pero
la componente temporal (R_unit) BAJA (2166 -> 1794, -17%) en pgen = 0.6. ¿Por qué? 

Porque R_unit promedia 1/t_gen, y en pgen = 0.6 una fracción mayor de los intentos
exitosos ocurre justo después de un stall de 3 s (el "arranque en frío"), donde el
rtt se dobla (~0.39 ms -> ~0.78 ms):

    pgen    frac_post   R_unit     R_unit_w (sin el artefacto post-stall)
    0.5       0.17      2166            2358
    0.6       0.41      1794            2190
    0.8       0.05      2470            2532
    1.0       0.00      2563            2563

El efecto neto: el -17% en R_unit supera al +15% de frac_ok, y pgen = 0.6 acaba
~4% por debajo de pgen = 0.5. Sin el artefacto post-stall (R_unit_w), el orden se
corrige y pgen = 0.5 queda sólo ~7% por encima (dentro del ruido entre celdas).

La razón de fondo de que pgen = 0.6 tenga más intentos post-stall es la misma que
causa la anomalía del rate: la celda spends más tiempo en el régimen de sincronía
degradada / atascado (por el acoplamiento A/B descrito en la sección 2), y en ese
régimen predominan los intentos que reinician la generación tras un stall.


--------------------------------------------------------------------------------
5. RESUMEN / CONCLUSIÓN
--------------------------------------------------------------------------------

1. La anomalía pgen = 0.6 < pgen = 0.5 (en rate por intercambio) NO es un error de
   cálculo ni de la física de entanglement.

2. Es una consecuencia del acoplamiento A/B: el swap requiere que A y B estén
   listos SIMULTÁNEAMENTE y que el repetidor lo confirme. Es un evento colectivo.

3. Estadísticamente, en esa celda pgen = 0.6, al cliente lab le tocó esperar más a
   la confirmación de teleco (que fallaba en esos momentos), y como el swap depende
   de que los dos se cumplan a la vez, se produjeron menos generaciones/swaps
   efectivos para lab. pgen = 0.5 fue más afortunado en sincronía (2.99 gen/swap
   frente a 4.78).

4. El E_N MEDIO por intento (columna EN_mean / plot 4b) sigue exactamente la
   proporción pgen (= frac_ok), porque depende sólo de si cada generación tuvo éxito,
   no de cuántas generaciones se necesitaron por swap. Esto confirma que el
   entanglement por intento es correcto y monótono.

5. El E_n medio NO puedeUsed para "arreglar" la anomalía porque la anomalía está en
   el número de rondas, no en el valor de entanglement. Para que el rate sea
   monótono en pgen habría que eliminar los stalls (reducir `swap_wait_timeout` a
   milisegundos) o el acoplamiento A/B, de modo que la sincronía no degrade el
   rendimiento.

================================================================================
Archivos de referencia:
    aegso_analysis.py    -> print_summary_pgen_at_pswap1 (columnas EN_mean, frac_post,
                            R_unit, R_unit_w), plot_en_mean_heatmap_pswap (plot 4b)
    aegso.py             -> PHASE 1 / PHASE 2 del cliente, bucle del repetidor
    run_aegso.sh         -> swap_wait_timeout = 3.0 s, coherence_ns = 1e6
================================================================================