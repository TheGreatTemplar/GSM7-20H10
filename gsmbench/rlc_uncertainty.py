"""E7-29 basic error limits: manual tables 2.2–2.6, pages 5–6.

Type B estimates assume a rectangular distribution within the specified limit.
They do not include repeatability, temperature or uncharacterized fixtures.
"""

import math


FIXTURES = [('Не указано', None), ('УП-2', 'UP-2'), ('УП-5', 'UP-5'), ('УП-9', 'UP-9'), ('Другое / самодельное', 'other')]
SOURCE = 'Е7-29 РЭ, таблицы 2.2–2.6, стр. 5–6'


def coefficient_a1(frequency_hz, impedance_ohm, range_index):
    limits = [(1e5, 1e8), (1e4, 1e5), (1e3, 1e4), (100, 1000), (10, 100), (1, 10), (.01, 1)]
    if type(range_index) is not int or not 0 <= range_index < len(limits):
        return None
    low, high = limits[range_index]
    if not low <= impedance_ohm <= high or not 50000 <= frequency_hz <= 15000000:
        return None
    z = impedance_ohm
    band = 0 if frequency_hz <= 100000 else 1 if frequency_hz <= 1000000 else 2
    mhz = frequency_hz / 1e6
    formulas = [
        [lambda: 1+.2*(z/1e5-1), None, None],
        [lambda: .5+.1*(z/1e4-1), lambda: 1+.2*(z/1e4-1), None],
        [lambda: .3+.05*(z/1e3-1), lambda: .5+.1*(z/1e3-1), lambda: (1+.2*(z/1e3-1))*mhz],
        [lambda: .2+.02*(z/100-1), lambda: .3+.05*(z/100-1), lambda: (.3+.05*(z/100-1))*mhz],
        [lambda: .2+.04*(100/z-1), lambda: .3+.05*(100/z-1), lambda: (.5+.1*(100/z-1))*mhz],
        [lambda: .5+.1*(10/z-1), lambda: 1+.2*(10/z-1), None],
        [lambda: 1+.2*(1/z-1), None, None],
    ]
    formula = formulas[range_index][band]
    return formula() if formula else None


def measurement_uncertainty(point, fixture_id, ac_level_v):
    result = dict(status='unavailable', source=SOURCE, model_version=1, fixture_id=fixture_id,
        operator_ac_level_v=ac_level_v, parameters={}, scope='Основная погрешность при нормальных условиях и прогреве не менее 15 минут.',
        distribution='rectangular', coverage_factor=2,
        excluded=['Повторяемость (тип A)', 'Дополнительная температурная погрешность',
                  'Неизвестные остаточные параметры оснастки и её коррекции'])

    def unavailable(reason):
        result['reason'] = reason
        return result

    if point.get('overloaded'):
        return unavailable('Перегрузка прибора')
    if fixture_id not in ('UP-2', 'UP-5', 'UP-9'):
        return unavailable('Не выбрано нормированное КУ: УП-2, УП-5 или УП-9')
    if ac_level_v is None or not math.isfinite(ac_level_v) or not .04 <= ac_level_v <= 1:
        return unavailable('Не указан уровень испытательного напряжения AC с панели (0,04…1 В)')
    frequency = point['frequency_hz']
    if fixture_id == 'UP-2' and frequency > 100000:
        return unavailable('Для УП-2 коэффициент A4 нормирован только до 100 кГц')
    values = point['values']
    if values.get('rs_ohm', -1) < 0:
        return unavailable('Отрицательная активная часть импеданса; формулы для пассивных ЭРЭ неприменимы')
    a1 = coefficient_a1(frequency, values['z_ohm'], point['range_index'])
    if a1 is None:
        return unavailable('Частота или |Z| вне нормированного участка фактически выбранного диапазона (табл. 2.3)')
    if point['speed'] not in (0, 1, 2):
        return unavailable('Неизвестный режим скорости')
    a2 = 10 if ac_level_v <= .1 else 3 if ac_level_v <= .3 else 1
    a3 = 3 if point['speed'] == 0 else 1
    a4 = 1.5+.015*(frequency/1000) if fixture_id == 'UP-2' else 1
    delta_z = a1*a2*a3*a4
    result.update(status='estimated', coefficients=dict(A1=a1, A2=a2, A3=a3, A4=a4),
                  impedance_error_limit_percent=delta_z)

    def add(key, absolute_limit, relative_percent=None):
        if values.get(key) is not None:
            result['parameters'][key] = dict(error_limit_abs=absolute_limit,
                error_limit_percent=relative_percent, standard_uncertainty_abs=absolute_limit/math.sqrt(3),
                expanded_uncertainty_k2_abs=2*absolute_limit/math.sqrt(3))

    add('z_ohm', values['z_ohm']*delta_z/100, delta_z)
    add('phase_deg', delta_z)
    q, d = values.get('q'), values.get('d')
    if q is not None:
        relative = delta_z*(1+q)
        for key in ('rs_ohm', 'rp_ohm', 'g_s'):
            if values.get(key) is not None:
                add(key, abs(values[key])*relative/100, relative)
        add('q', delta_z/100*((1+q) if q <= 1 else (q+q*q)))
    if d is not None:
        relative = delta_z*(1+d)
        for key in ('ls_h', 'lp_h', 'cs_f', 'cp_f', 'xs_ohm', 'b_s'):
            if values.get(key) is not None:
                add(key, abs(values[key])*relative/100, relative)
        add('d', delta_z/100*((1+d) if d <= 1 else (1+d*d)))
    return result
