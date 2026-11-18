"""Shared names for physical RF settings shown in more than one workspace."""

INPUT_ATTENUATION_A = "Ослабление тракта A, дБ"
INPUT_ATTENUATION_B = "Ослабление тракта B, дБ"
INPUT_ATTENUATION_HINT = (
    "Суммарное ослабление от выхода SMU {channel} до выхода комбинера: кабель + аттенюатор + комбинер. "
    "Например, аттенюатор 10 дБ и комбинер около 6 дБ дают начальную оценку 16 дБ. "
    "Это один параметр для калибровки и двухтонового измерения; значение общее на обеих вкладках."
)
RSA_ATTENUATION = "Ослабление до RSA, дБ"
AFTER_COMBINER_ATTENUATION = "После комбинера, дБ"
SETTLING_TIME = "Установление, с"
SMU_LEVEL = "Уровень SMU, dBm"
