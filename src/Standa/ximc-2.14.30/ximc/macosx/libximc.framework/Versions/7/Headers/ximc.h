#ifndef INC_XIMC_H
#define INC_XIMC_H

/** @file ximc.h
	* \english
	*		@brief Header file for libximc library
	* \endenglish
	* \russian
	*		@brief Заголовочный файл для библиотеки libximc
	* \endrussian
	*/

/** @def XIMC_API
	* \english
	* 		@brief Library import macro.
	* 		Macros allows to automatically import function from shared library.
	* 		It automatically expands to dllimport on msvc when including header file.
	* \endenglish
	* \russian
	*		@brief Макрос импорта библиотеки.
	*		Макросы позволяют автоматически импортировать функцию из общей библиотеки.
	*		Он автоматически расширяется до dllimport на msvc при включении файла заголовка.
	* \endrussian
	*/
#if defined(_WIN32) || defined(LABVIEW64_IMPORT) || defined(LABVIEW32_IMPORT) || defined(MATLAB_IMPORT)
	#define XIMC_API __stdcall
#else
	#ifdef LIBXIMC_EXPORTS
	#define XIMC_API __attribute__((visibility("default")))
	#else
	#define XIMC_API
	#endif
#endif

/** @def XIMC_CALLCONV
	* \english
	*		@brief Library calling convention macros.
	* \endenglish
	* \russian
	*		@brief Библиотека вызывающая условные макросы.
	* \endrussian
	*/
#if defined(_WIN32) || defined(LABVIEW64_IMPORT) || defined(LABVIEW32_IMPORT) || defined(MATLAB_IMPORT)
	#define XIMC_CALLCONV __stdcall
#else
	#define XIMC_CALLCONV
#endif

/** @def XIMC_RETTYPE
	* \english
	* 		@brief Thread return type.
	* \endenglish
	* \russian
	*		@brief Возвращаеемый тип потока.
	* \endrussian
	*/
#if defined(_WIN32) || defined(LABVIEW64_IMPORT) || defined(LABVIEW32_IMPORT) || defined(MATLAB_IMPORT)
#define XIMC_RETTYPE unsigned int
#else
#define XIMC_RETTYPE void*
#endif


#if !defined(XIMC_NO_STDINT)

#if ( (defined(_MSC_VER) && (_MSC_VER < 1600)) || defined(LABVIEW64_IMPORT) || defined(LABVIEW32_IMPORT)) && !defined(MATLAB_IMPORT)
// msvc types burden
typedef __int8 int8_t;
typedef __int16 int16_t;
typedef __int32 int32_t;
typedef __int64 int64_t;
typedef unsigned __int8 uint8_t;
typedef unsigned __int16 uint16_t;
typedef unsigned __int32 uint32_t;
typedef unsigned __int64 uint64_t;
#else
#include <stdint.h>
#endif

/* labview doesn't speak C99 */
#if defined(LABVIEW64_IMPORT) || defined(LABVIEW32_IMPORT)
typedef unsigned __int64 ulong_t;
typedef __int64 long_t;
#else
typedef unsigned long long ulong_t;
typedef long long long_t;
#endif

#endif

#include <time.h>

#if defined(__cplusplus)
extern "C"
{
#endif


	/**
		\english
		* Type describes device identifier
		\endenglish
		\russian
		* Тип идентификатора устройства
		\endrussian
		*/
	typedef int device_t;

	/**
		\english
		* Type specifies result of any operation
		\endenglish
		\russian
		* Тип, определяющий результат выполнения команды.
		\endrussian
		*/
	typedef int result_t;

	/**
		\english
		* Type describes device enumeration structure
		\endenglish
		\russian
		* Тип, определяющий структуру данных о всех контроллерах, обнаруженных при опросе устройств.
		\endrussian
		*/
	#if defined(_WIN64) || defined(__LP64__) || defined(LABVIEW64_IMPORT)
	typedef uint64_t device_enumeration_t;
	#else
	typedef uint32_t device_enumeration_t;
	#endif
	//typedef device_enumeration_t* pdevice_enumeration_t;

	/**
		\english
		* Handle specified undefined device
		\endenglish
		\russian
		* Макрос, означающий неопределенное устройство
		\endrussian
		*/
#define device_undefined -1

	/** \english
		@name Result statuses
		\endenglish
		\russian
		@name Результаты выполнения команд
		\endrussian
		*/
	//@{

	/**
		\english
		* success
		\endenglish
		\russian
		* выполнено успешно
		\endrussian
		*/
#define result_ok 0

	/**
		\english
		* generic error
		\endenglish
		\russian
		* общая ошибка
		\endrussian
		*/
#define result_error -1

	/**
		\english
		* function is not implemented
		\endenglish
		\russian
		* функция не определена
		\endrussian
		*/
#define result_not_implemented -2

	/**
		\english
		* value error
		\endenglish
		\russian
		* ошибка записи значения
		\endrussian
		*/
#define result_value_error -3

	/**
		\english
		* device is lost
		\endenglish
		\russian
		* устройство не подключено
		\endrussian
		*/
#define result_nodevice -4

	//@}

	/** \english
		@name Logging level
		\endenglish
		\russian
		@name Уровень логирования
		\endrussian
		*/
	//@{

	/**
		\english
		* Logging level - error
		\endenglish
		\russian
		* Уровень логирования - ошибка
		\endrussian
		*/
#define LOGLEVEL_ERROR 		0x01
	/**
		\english
		* Logging level - warning
		\endenglish
		\russian
		* Уровень логирования - предупреждение
		\endrussian
		*/
#define LOGLEVEL_WARNING 	0x02
	/**
		\english
		* Logging level - info
		\endenglish
		\russian
		* Уровень логирования - информация
		\endrussian
		*/
#define LOGLEVEL_INFO		0x03
	/**
		\english
		* Logging level - debug
		\endenglish
		\russian
		* Уровень логирования - отладка
		\endrussian
		*/
#define LOGLEVEL_DEBUG		0x04
	//@}


    /**
		\english
		* Calibration structure
		*
		* Where to find all values for calculations?
		* - XILab (don’t forget to load the profile for your positioner. The profile should match the full name of your positioner, e.g., 8MT173-25-MEn1.cfg):
		*   - In XILab settings, go to the "User units" tab. Divide the second number by the first one — this is your A coefficient.
		*   - In the "DC motor" / "BLDC motor" / "Stepper motor" tab (depending on your motor type), find the "Encoder counts per turn" field and use it in the formula for calculating coefficient B.
		* - Profile file (open with any text editor; make sure it matches the full name of your positioner, e.g., 8MT173-25-MEn1.cfg):
		*   - Find Step_multiplier= and Unit_multiplier=. Divide the second by the first — this is your A coefficient.
		*   - Find Encoder_CPT= and use this value in the B coefficient formula instead of ENCODER_COUNTS_PER_TURN.

		* How to calculate Speed, Accel, Decel, and AntiplaySpeed in user units when using a stepper motor with encoder or DC/BLDC motor?
		* 1. Load the correct profile in XILab for your positioner (e.g., 8MT173-25-MEn1.cfg).
		* 2. Enable Feedback encoder mode if not already enabled.
		* 3. Enter speed in the "Working speed" field in user units.
		* 4. In the "User units" tab, disable the "User units" flag to see RPM.
		* 5. Multiply the RPM value from "Working speed" by coefficient B.
		*    Example: 480 * 0.0000009375 = 0.00045. This value for the 8MT173-25-MEn1 in Encoder mode equals 2 mm/s.
		*
		*    Acceleration, deceleration, and antiplay speed are calculated the same way.
		\endenglish

		\russian
		* Структура калибровок
		*
		* Где найти все значения для расчета? 
		* - XILab (не забудьте загрузить профиль для вашего позиционера. Профиль должен соответствовать полному названию вашего позиционера. Например: 8MT173-25-MEn1.cfg): 
		*   - В настройках XILab перейдите во вкладку user units. Разделите второе число на первое — это и будет коэффициент A
		*   - В настройках XILab, перейдите во вкладку DC motor/BLDC motor/Stepper motor (зависит от используемого типа двигателя). Подставьте значение из поля Encoder counts per turn в формулу подсчета B коэффициента
		* - Профиль (откройте профиль любым текстовым редактором. Профиль должен соответствовать полному названию вашего позиционера. Например: 8MT173-25-MEn1.cfg): 
		*   - Найдите в файле профиля поля Step_multiplier= и Unit_multiplier=. Разделите второе число на первое — это и будет коэффициент A
		*   - Найдите в файле профиля поле Encoder_CPT=. Подставьте значение из этого поля в формулу подсчета B коэффициента вместо ENCODER_COUNTS_PER_TURN

		* Как посчитать Speed, Accel, Decel и AntiplaySpeed в пользовательских единицах при использовании шагового двигателя с энкодером или DC/BLDC двигателей? 
		* 1. Используя XILab, загрузите профиль для вашего позиционера. Профиль должен соответствовать полному названию вашего позиционера. Например: 8MT173-25-MEn1.cfg
		* 2. Включите режим Feedback encoder, если он не был включен ранее
		* 3. Вводите скорость в пользовательских единицах в поле Working speed
		* 4. Во вкладке User units выключите флаг User units. Это позволит видеть значение в поле Working speed в RPM
		* 5. Умножьте значение из поля Working speed (в RPM) на коэффициент B. Например: 480 * 0.0000009375 = 0.00045. Значение 0.00045 для позиционера 8MT173-25-MEn1 в режиме Encoder будет равно скорости 2 мм/сек
		*
		* Ускорение, замедление и скорость в режиме антилюфта считаются аналогично
		\endrussian	*/
	typedef struct calibration_t
	{
		double A; /**<
		 * \english
		 * Conversion coefficient equal to the number of millimeters (or other user units) per one step.
		 * Must be non-zero and positive.
		 * Used for position and movement conversion.
		 * - For stepper motor without encoder, only one coefficient A is used. Conversion formula: [user_unit/steps].
		 *   Example: 800 steps = 1 mm, then A = 1/800 = 0.00125
		 * - When using a stepper motor with encoder or DC/BLDC motors, the position is set in counts,
		 *   but speed, acceleration/deceleration, and antiplay speed are set in RPM,
		 *   so two coefficients are required:
		 *   - A. Conversion for position: [user_unit/counts]. Example: 16000 counts = 1 mm, then A = 1/16000 = 0.0000625
		 *   - B. Conversion for speed, acceleration/deceleration, and antiplay speed: [60/ENCODER_COUNTS_PER_TURN * A].
		 *     Example: 60 / 4000 * 0.0000625 = 0.0000009375
		 * \endenglish
		 * \russian
		 * Коэффициент преобразования, равный количеству миллиметров (или других пользовательских единиц) на один шаг.
		 * Должен быть отличным от нуля и положительным.
		 * Используется для пересчёта положений и перемещений.
		 * - Для шагового двигателя без энкодера используется только один коэффициент A. Формула пересчёта: [user_unit/steps].
		 *   Пример: 800 шагов = 1 мм, тогда A = 1/800 = 0.00125
		 * - При использовании шагового двигателя с энкодером или двигателей DC/BLDC позиция задаётся в counts,
		 *   но скорость, ускорение/замедление и скорость в режиме антилюфта задаются в RPM,
		 *   поэтому требуются два коэффициента:
		 *   - A. Формула пересчёта для позиции: [user_unit/counts]. Пример: 16000 counts = 1 мм, тогда A = 1/16000 = 0.0000625
		 *   - B. Формула пересчёта для скорости, ускорения/замедления и скорости в режиме антилюфта: [60/ENCODER_COUNTS_PER_TURN * A].
		 *     Пример: 60 / 4000 * 0.0000625 = 0.0000009375
		 * \endrussian
		 */
		unsigned int MicrostepMode;			/**< \english Controller setting which is determine a step division mode \endenglish \russian Настройка контроллера, определяющая режим пошагового деления. \endrussian */
	} calibration_t;

	/**
		\english
		* Device network information structure.
		\endenglish
		\russian
		* Структура данных с информацией о сетевом устройстве.
		\endrussian	 */
	typedef struct device_network_information_t
	{
		uint32_t ipv4; 		/**< \english IPv4 address, passed in network byte order (big-endian byte order) \endenglish \russian IPv4-адрес, передаваемый в сетевом байтовом порядке (big-endian byte order) \endrussian */
		char nodename[16]; 		/**< \english name of the Bindy node which hosts the device \endenglish \russian имя узла Bindy, на котором размещено устройство \endrussian */
		
		uint32_t axis_state; 		/**< \english flags representing device state \endenglish \russian флаги, представляющие состояние устройства \endrussian */
		char locker_username[16]; 		/**< \english name of the user who locked the device (if any) \endenglish \russian имя пользователя, заблокировавшего устройство (если таковое имеется) \endrussian */
		char locker_nodename[16]; 		/**< \english Bindy node name, which was used to lock the device (if any) \endenglish \russian имя узла Bindy, которое использовалось для блокировки устройства (если таковое имеется) \endrussian */
		time_t locked_time; 		/**< \english time the lock was acquired at (UTC, microseconds since the epoch) \endenglish \russian время, в которое была получена блокировка (UTC, микросекунды с момента начала эпохи) \endrussian */
	} device_network_information_t;



/** @cond DO_NOT_WANT */
#define LIBXIMC_VERSION 2.14.30
/** @endcond */


/** @cond DO_NOT_WANT */
#define LIBXIMC_PROTOCOL_VERSION 20.9
/** @endcond */


/*
 ------------------------------------------
   BEGIN OF GENERATED struct declarations
 ------------------------------------------
*/

/** \english
	* @anchor flagset_enumerateflags
	@name Enumerate devices flags
	* This is a bit mask for bitwise operations.
	\endenglish
	\russian
	@name Флаги поиска устройств
	* Это битовая маска для побитовых операций.
	\endrussian
	*/
//@{
#define ENUMERATE_PROBE     0x01 	/**< \english Check if a device with an OS name is a XIMC device. Be careful with this flag because it sends some data to the device. \endenglish \russian Проверять, является ли устройство XIMC-совместимым. Будьте осторожны с этим флагом, т.к. он отправляет данные в устройство.  \endrussian */
#define ENUMERATE_ALL_COM   0x02 	/**< \english Check all COM devices \endenglish \russian Проверять все COM-устройства \endrussian */
#define ENUMERATE_NETWORK   0x04 	/**< \english Check network devices \endenglish \russian Проверять сетевые устройства \endrussian */
//@}


/**
	* @anchor flagset_movestate
	* \english
	* @name Flags of move state
	* This is a bit mask for bitwise operations.
	* Specify move states.
	* \endenglish
	* \russian
	* @name Флаги состояния движения
	* Это битовая маска для побитовых операций.
	* Возвращаются командой get_status.
	* \endrussian
	* @see get_status
	* @see status_t::MoveSts, get_status_impl
	*/
//@{
#define MOVE_STATE_MOVING         0x01 	/**< \english This flag indicates that the controller is trying to move the motor. Don't use this flag to wait for the completion of the movement command. Use the MVCMD_RUNNING flag from the MvCmdSts field instead. \endenglish \russian Если флаг установлен, то контроллер пытается вращать двигателем. Не используйте этот флаг для ожидания завершения команды движения. Вместо него используйте MVCMD_RUNNING из поля MvCmdSts. \endrussian */
#define MOVE_STATE_TARGET_SPEED   0x02 	/**< \english Target speed is reached, if flag set. \endenglish \russian Флаг устанавливается при достижении заданной скорости. \endrussian */
#define MOVE_STATE_ANTIPLAY       0x04 	/**< \english Motor is playing compensation, if flag set. \endenglish \russian Выполняется компенсация люфта, если флаг установлен. \endrussian */
//@}


/**
	* @anchor flagset_controllerflags
	* \english
	* @name Flags of internal controller settings
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги настроек контроллера
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see set_controller_name
	* @see get_controller_name
	* @see controller_name_t::CtrlFlags, get_controller_name, set_controller_name
	*/
//@{
#define EEPROM_PRECEDENCE   0x01 	/**< \english If the flag is set, settings from external EEPROM override controller settings. \endenglish \russian Если флаг установлен, то настройки в EEPROM подвижки имеют приоритет над текущими настройками и заменяют их при обнаружении EEPROM. \endrussian */
//@}


/**
	* @anchor flagset_powerstate
	* \english
	* @name Flags of power state of stepper motor
	* This is a bit mask for bitwise operations.
	* Specify power states.
	* \endenglish
	* \russian
	* @name Флаги состояния питания шагового мотора
	* Это битовая маска для побитовых операций.
	* Возвращаются командой get_status.
	* \endrussian
	* @see get_status
	* @see status_t::PWRSts, get_status_impl
	*/
//@{
#define PWR_STATE_UNKNOWN   0x00 	/**< \english Unknown state, should never happen. \endenglish \russian Неизвестное состояние, которое не должно никогда реализовываться. \endrussian */
#define PWR_STATE_OFF       0x01 	/**< \english Motor windings are disconnected from the driver. \endenglish \russian Обмотки мотора разомкнуты и не управляются драйвером. \endrussian */
#define PWR_STATE_NORM      0x03 	/**< \english Motor windings are powered by nominal current. \endenglish \russian Обмотки запитаны номинальным током. \endrussian */
#define PWR_STATE_REDUCT    0x04 	/**< \english Motor windings are powered by reduced current to lower power consumption. \endenglish \russian Обмотки намеренно запитаны уменьшенным током от рабочего для снижения потребляемой мощности. \endrussian */
#define PWR_STATE_MAX       0x05 	/**< \english Motor windings are powered by the maximum current driver can provide at this voltage. \endenglish \russian Обмотки двигателя питаются от максимального тока, который драйвер может обеспечить при этом напряжении. \endrussian */
//@}


/**
	* @anchor flagset_stateflags
	* \english
	* @name Status flags
	* This is a bit mask for bitwise operations.
	* Controller flags returned by device query.
	* Contains boolean part of controller state.
	* May be combined with bitwise OR.
	* \endenglish
	* \russian
	* @name Флаги состояния
	* Это битовая маска для побитовых операций.
	* Содержат бинарные значения состояния контроллера. Могут быть объединены с помощью логического ИЛИ.
	* \endrussian
	* @see get_status
	* @see status_t::Flags, get_status_impl
	*/
//@{
#define STATE_CONTR                    0x000003F 	/**< \english Flags of controller states. \endenglish \russian Флаги состояния контроллера. \endrussian */
#define STATE_ERRC                     0x0000001 	/**< \english Command error encountered. The command received is not in the list of controller known commands. The most possible reason is the outdated firmware. \endenglish \russian Недопустимая команда. Полученная команда отсутствует в списке известных команд контроллера. Наиболее вероятной причиной является устаревшая прошивка. \endrussian */
#define STATE_ERRD                     0x0000002 	/**< \english Data integrity error encountered. The data inside the command and its CRC code do not correspond. Therefore, the data can't be considered valid. This error may be caused by EMI in the UART/RS232 interface. \endenglish \russian Обнаружена ошибка целостности данных. Данные внутри команды и ее CRC-код не соответствуют, поэтому данные не могут считаться действительными. Эта ошибка может быть вызвана электромагнитными помехами в интерфейсе UART/RS232. \endrussian */
#define STATE_ERRV                     0x0000004 	/**< \english Value error encountered. The values in the command can't be applied without correction because they fall outside the valid range. Corrected values were used instead of the original ones. \endenglish \russian Недопустимое значение данных. Обнаружена ошибка в значении. Значения в команде не могут быть применены без коррекции, поскольку они выходят за допустимый диапазон. Вместо исходных значений были использованы исправленные значения. \endrussian */
#define STATE_EEPROM_CONNECTED         0x0000010 	/**< \english EEPROM with settings is connected. The built-in stage profile is uploaded from the EEPROM memory chip if the EEPROM_PRECEDENCE flag is set, allowing you to connect various stages to the controller with automatic setup. \endenglish \russian Подключена память EEPROM с настройками. Встроенный профиль подвижки загружается из микросхемы памяти EEPROM, что позволяет подключать различные подвижки к контроллеру с автоматической настройкой. \endrussian */
#define STATE_IS_HOMED                 0x0000020 	/**< \english Calibration performed. This means that the relative position scale is calibrated against a hardware absolute position sensor, like a limit switch. Drops after loss of calibration, like harsh stops and possibly skipped steps. \endenglish \russian Калибровка выполнена. Это означает, что шкала относительного положения откалибрована с помощью аппаратного датчика абсолютного положения, такого как концевой переключатель. \endrussian */
#define STATE_SECUR                    0x1B3FFC0 	/**< \english Security flags. \endenglish \russian Флаги опасности. \endrussian */
#define STATE_ALARM                    0x0000040 	/**< \english The controller is in an alarm state, indicating that something dangerous has happened. Most commands are ignored in this state. To reset the flag, a STOP command must be issued. \endenglish \russian Контроллер находится в состоянии ALARM, показывая, что случилась какая-то опасная ситуация. В состоянии ALARM все команды игнорируются пока не будет послана команда STOP и состояние ALARM деактивируется. \endrussian */
#define STATE_CTP_ERROR                0x0000080 	/**< \english Control position error (is only used with stepper motor). The flag is set when the encoder position and step position are too far apart. \endenglish \russian Контроль позиции нарушен(используется только с шаговым двигателем). Флаг устанавливается, когда положение энкодера и положение шага слишком далеки друг от друга. \endrussian */
#define STATE_POWER_OVERHEAT           0x0000100 	/**< \english Power driver overheat. Motor control is disabled until some cooldown occurs. This should not happen with boxed versions of the controller. This may happen with the bare-board version of the controller with a custom radiator. Redesign your radiator. \endenglish \russian Перегрев силового драйвера. Управление двигателем отключено до восстановления рабочей температуры драйвера. Этого не должно происходить в коробочных версиях контроллера. Это может произойти в версии контроллера с «голой» платой и с пользовательским радиатором. Решение: используйте другой радиатор. \endrussian */
#define STATE_CONTROLLER_OVERHEAT      0x0000200 	/**< \english Controller overheat. \endenglish \russian Перегрелась микросхема контроллера. \endrussian */
#define STATE_OVERLOAD_POWER_VOLTAGE   0x0000400 	/**< \english Power voltage exceeds safe limit. \endenglish \russian Превышено напряжение на силовой части. \endrussian */
#define STATE_OVERLOAD_POWER_CURRENT   0x0000800 	/**< \english Power current exceeds safe limit. \endenglish \russian Превышен максимальный ток потребления силовой части. \endrussian */
#define STATE_OVERLOAD_USB_VOLTAGE     0x0001000 	/**< \english Deprecated. USB voltage exceeds safe limit. This field is left for compatibility. Software should not rely on the value of this field. \endenglish \russian Устарело. Превышено напряжение на USB. Это поле оставлено для совместимости. Программное обеспечение не должно полагаться на значение этого поля. \endrussian */
#define STATE_LOW_USB_VOLTAGE          0x0002000 	/**< \english Deprecated. USB voltage is insufficient for normal operation. This field is left for compatibility. Software should not rely on the value of this field. \endenglish \russian Устарело. Слишком низкое напряжение на USB. Это поле оставлено для совместимости. Программное обеспечение не должно полагаться на значение этого поля. \endrussian */
#define STATE_OVERLOAD_USB_CURRENT     0x0004000 	/**< \english Deprecated. USB current exceeds safe limit. This field is left for compatibility. Software should not rely on the value of this field. \endenglish \russian Устарело. Превышен максимальный ток потребления USB. Это поле оставлено для совместимости. Программное обеспечение не должно полагаться на значение этого поля. \endrussian */
#define STATE_BORDERS_SWAP_MISSET      0x0008000 	/**< \english Engine stuck at the wrong edge. \endenglish \russian Достижение неверной границы. \endrussian */
#define STATE_LOW_POWER_VOLTAGE        0x0010000 	/**< \english Power voltage is lower than Low Voltage Protection limit \endenglish \russian Напряжение на силовой части ниже чем напряжение Low Voltage Protection \endrussian */
#define STATE_H_BRIDGE_FAULT           0x0020000 	/**< \english Signal from the driver that fault happened \endenglish \russian Получен сигнал от драйвера о неисправности \endrussian */
#define STATE_WINDING_RES_MISMATCH     0x0100000 	/**< \english The difference between winding resistances is too large. This usually happens with a damaged stepper motor with partially short-circuited windings. \endenglish \russian Сопротивления обмоток слишком сильно отличаются друг от друга. Обычно это происходит с поврежденным шаговым двигателем у которого полностью или частично закорочены обмотки. \endrussian */
#define STATE_ENCODER_FAULT            0x0200000 	/**< \english Signal from the encoder that fault happened \endenglish \russian Получен сигнал от энкодера о неисправности \endrussian */
#define STATE_ENGINE_RESPONSE_ERROR    0x0800000 	/**< \english Error response of the engine control action. Motor control algorithm failure means that it can't make the correct decisions with the feedback data it receives. A single failure may be caused by a mechanical problem. A repeating failure can be caused by incorrect motor settings. \endenglish \russian Ошибка реакции двигателя на управляющее воздействие. Отказ алгоритма управления двигателем означает, что он не может определять правильные решения с помощью полученных данных обратной связи. Единичный отказ может быть вызван механической проблемой. Повторяющийся сбой может быть вызван неправильной настройкой двигателя. \endrussian */
#define STATE_EXTIO_ALARM              0x1000000 	/**< \english The error is caused by the external EXTIO input signal. \endenglish \russian Ошибка вызвана внешним входным сигналом EXTIO. \endrussian */
//@}


/**
	* @anchor flagset_gpioflags
	* \english
	* @name Status flags of the GPIO outputs
	* This is a bit mask for bitwise operations.
	* GPIO state flags returned by device query.
	* Contains boolean part of controller state.
	* May be combined with bitwise OR.
	* \endenglish
	* \russian
	* @name Флаги состояния GPIO входов
	* Это битовая маска для побитовых операций.
	* Содержат бинарные значения состояния контроллера. Могут быть объединены с помощью логического ИЛИ.
	* \endrussian
	* @see get_status
	* @see status_t::GPIOFlags, get_status_impl
	*/
//@{
#define STATE_DIG_SIGNAL     0xFFFF 	/**< \english Flags of digital signals. \endenglish \russian Флаги цифровых сигналов. \endrussian */
#define STATE_RIGHT_EDGE     0x0001 	/**< \english Engine stuck at the right edge. \endenglish \russian Достижение правой границы. \endrussian */
#define STATE_LEFT_EDGE      0x0002 	/**< \english Engine stuck at the left edge. \endenglish \russian Достижение левой границы. \endrussian */
#define STATE_BUTTON_RIGHT   0x0004 	/**< \english Button "right" state (1 if pressed). \endenglish \russian Состояние кнопки "вправо" (1, если нажата). \endrussian */
#define STATE_BUTTON_LEFT    0x0008 	/**< \english Button "left" state (1 if pressed). \endenglish \russian Состояние кнопки "влево" (1, если нажата). \endrussian */
#define STATE_GPIO_PINOUT    0x0010 	/**< \english External GPIO works as out if the flag is set; otherwise, it works as in. \endenglish \russian Если флаг установлен, ввод/вывод общего назначения работает как выход; если флаг сброшен, ввод/вывод работает как вход. \endrussian */
#define STATE_GPIO_LEVEL     0x0020 	/**< \english State of external GPIO pin. \endenglish \russian Состояние ввода/вывода общего назначения. \endrussian */
#define STATE_BRAKE          0x0200 	/**< \english State of Brake pin. Flag "1" - if the pin state brake is not powered (brake is clamped), "0" - if the pin state brake is powered (brake is unclamped). \endenglish \russian Состояние вывода управления тормозом. Флаг "1" - если тормоз не запитан(зажат), "0" - если на тормоз подаётся питание(разжат). \endrussian */
#define STATE_REV_SENSOR     0x0400 	/**< \english State of Revolution sensor pin. \endenglish \russian Состояние вывода датчика оборотов(флаг "1", если датчик активен). \endrussian */
#define STATE_SYNC_INPUT     0x0800 	/**< \english State of Sync input pin. \endenglish \russian Состояние входа синхронизации(1, если вход синхронизации активен). \endrussian */
#define STATE_SYNC_OUTPUT    0x1000 	/**< \english State of Sync output pin. \endenglish \russian Состояние выхода синхронизации(1, если выход синхронизации активен). \endrussian */
#define STATE_ENC_A          0x2000 	/**< \english State of encoder A pin. \endenglish \russian Состояние ножки A энкодера(флаг "1", если энкодер активен). \endrussian */
#define STATE_ENC_B          0x4000 	/**< \english State of encoder B pin. \endenglish \russian Состояние ножки B энкодера(флаг "1", если энкодер активен). \endrussian */
//@}


/**
	* @anchor flagset_encodestatus
	* \english
	* @name Encoder state
	* This is a bit mask for bitwise operations.
	* Encoder state returned by device query.
	* \endenglish
	* \russian
	* @name Состояние энкодера
	* Это битовая маска для побитовых операций.
	* Состояние энкодера, подключенного к контроллеру.
	* \endrussian
	* @see get_status
	* @see status_t::EncSts, get_status_impl
	*/
//@{
#define ENC_STATE_ABSENT    0x00 	/**< \english Encoder is absent. \endenglish \russian Энкодер не подключен. \endrussian */
#define ENC_STATE_UNKNOWN   0x01 	/**< \english Encoder state is unknown. \endenglish \russian Состояние энкодера неизвестно. \endrussian */
#define ENC_STATE_MALFUNC   0x02 	/**< \english Encoder is connected and malfunctioning. \endenglish \russian Энкодер подключен и неисправен. \endrussian */
#define ENC_STATE_REVERS    0x03 	/**< \english Encoder is connected and operational but counts in other direction. \endenglish \russian Энкодер подключен и исправен, но считает в другую сторону. \endrussian */
#define ENC_STATE_OK        0x04 	/**< \english Encoder is connected and working properly. \endenglish \russian Энкодер подключен и работает должным образом. \endrussian */
//@}


/**
	* @anchor flagset_windstatus
	* \english
	* @name Winding state
	* This is a bit mask for bitwise operations.
	* Motor winding state returned by device query.
	* \endenglish
	* \russian
	* @name Состояние обмоток
	* Это битовая маска для побитовых операций.
	* Состояние обмоток двигателя, подключенного к контроллеру.
	* \endrussian
	* @see get_status
	* @see status_t::WindSts, get_status_impl
	*/
//@{
#define WIND_A_STATE_ABSENT    0x00 	/**< \english Winding A is disconnected. \endenglish \russian Обмотка A не подключена. \endrussian */
#define WIND_A_STATE_UNKNOWN   0x01 	/**< \english Winding A state is unknown. \endenglish \russian Состояние обмотки A неизвестно. \endrussian */
#define WIND_A_STATE_MALFUNC   0x02 	/**< \english Winding A is short-circuited. \endenglish \russian Короткое замыкание на обмотке A. \endrussian */
#define WIND_A_STATE_OK        0x03 	/**< \english Winding A is connected and working properly. \endenglish \russian Обмотка A работает адекватно. \endrussian */
#define WIND_B_STATE_ABSENT    0x00 	/**< \english Winding B is disconnected. \endenglish \russian Обмотка B не подключена. \endrussian */
#define WIND_B_STATE_UNKNOWN   0x10 	/**< \english Winding B state is unknown. \endenglish \russian Состояние обмотки B неизвестно. \endrussian */
#define WIND_B_STATE_MALFUNC   0x20 	/**< \english Winding B is short-circuited. \endenglish \russian Короткое замыкание на обмотке B. \endrussian */
#define WIND_B_STATE_OK        0x30 	/**< \english Winding B is connected and working properly. \endenglish \russian Обмотка B работает адекватно. \endrussian */
//@}


/**
	* @anchor flagset_mvcmdstatus
	* \english
	* @name Move command state
	* This is a bit mask for bitwise operations.
	* Move command (command_move, command_movr, command_left, command_right, command_stop, command_home, command_loft, command_sstp)
	* and its state (run, finished, error).
	* \endenglish
	* \russian
	* @name Состояние команды движения
	* Это битовая маска для побитовых операций.
	* Состояние команды движения (касается command_move, command_movr, command_left, command_right, command_stop, command_home, command_loft, command_sstp)
	* и статуса её выполнения (выполняется, завершено, ошибка)
	* \endrussian
	* @see get_status
	* @see status_t::MvCmdSts, get_status_impl
	*/
//@{
#define MVCMD_NAME_BITS   0x3F 	/**< \english Move command bit mask. \endenglish \russian Битовая маска активной команды. \endrussian */
#define MVCMD_UKNWN       0x00 	/**< \english Unknown command. \endenglish \russian Неизвестная команда. \endrussian */
#define MVCMD_MOVE        0x01 	/**< \english Command move. \endenglish \russian Команда move. \endrussian */
#define MVCMD_MOVR        0x02 	/**< \english Command movr. \endenglish \russian Команда movr. \endrussian */
#define MVCMD_LEFT        0x03 	/**< \english Command left. \endenglish \russian Команда left. \endrussian */
#define MVCMD_RIGHT       0x04 	/**< \english Command rigt. \endenglish \russian Команда rigt. \endrussian */
#define MVCMD_STOP        0x05 	/**< \english Command stop. \endenglish \russian Команда stop. \endrussian */
#define MVCMD_HOME        0x06 	/**< \english Command home. \endenglish \russian Команда home. \endrussian */
#define MVCMD_LOFT        0x07 	/**< \english Command loft. \endenglish \russian Команда loft. \endrussian */
#define MVCMD_SSTP        0x08 	/**< \english Command soft stop. \endenglish \russian Команда плавной остановки(SSTP). \endrussian */
#define MVCMD_ERROR       0x40 	/**< \english Finish state (1 - move command has finished with an error, 0 - move command has finished correctly). This flag makes sense when MVCMD_RUNNING signals movement completion. \endenglish \russian Состояние завершения движения (1 - команда движения выполнена с ошибкой, 0 - команда движения выполнена корректно). Имеет смысл если MVCMD_RUNNING указывает на завершение движения. \endrussian */
#define MVCMD_RUNNING     0x80 	/**< \english Move command state (0 - move command has finished, 1 - move command is being executed). \endenglish \russian Состояние команды движения (0 - команда движения выполнена, 1 - команда движения сейчас выполняется). \endrussian */
//@}


/**
	* @anchor flagset_moveflags
	* \english
	* @name Flags of the motion parameters
	* This is a bit mask for bitwise operations.
	* Specify the motor shaft movement algorithm and list of limitations.
	* Flags returned by the query of get_move_settings.
	* \endenglish
	* \russian
	* @name Флаги параметров движения
	* Это битовая маска для побитовых операций.
	* Определяют настройки параметров движения.
	* Возвращаются командой get_move_settings.
	* \endrussian
	* @see set_move_settings
	* @see get_move_settings
	* @see move_settings_t::MoveFlags, get_move_settings, set_move_settings
	*/
//@{
#define RPM_DIV_1000   0x01 	/**< \english This flag indicates that the operating speed specified in the command is set in milliRPM. Applicable only for ENCODER feedback mode and only for BLDC motors. \endenglish \russian Флаг указывает на то что рабочая скорость указанная в команде задана в милли rpm. Применим только для режима обратной связи ENCODER и только для BLDC моторов. \endrussian */
//@}


/**
	* @anchor flagset_engineflags
	* \english
	* @name Flags of engine settings
	* This is a bit mask for bitwise operations.
	* Specify the motor shaft movement algorithm and list of limitations.
	* Flags returned by query of engine settings. May be combined with bitwise OR.
	* \endenglish
	* \russian
	* @name Флаги параметров мотора
	* Это битовая маска для побитовых операций.
	* Определяют настройки движения и работу ограничителей.
	* Возвращаются командой get_engine_settings. Могут быть объединены с помощью логического ИЛИ.
	* \endrussian
	* @see set_engine_settings
	* @see get_engine_settings
	* @see engine_settings_t::EngineFlags, get_engine_settings, set_engine_settings
	*/
//@{
#define ENGINE_REVERSE          0x01 	/**< \english Reverse flag. It determines motor shaft rotation direction that corresponds to feedback counts increasing. If not set (default), motor shaft rotation direction under positive voltage corresponds to feedback counts increasing and vice versa. Change it if you see that positive directions on motor and feedback are opposite. \endenglish \russian Флаг реверса. Связывает направление вращения мотора с направлением счета текущей позиции. При сброшенном флаге (по умолчанию) прикладываемое к мотору положительное напряжение увеличивает счетчик позиции. И наоборот, при установленном флаге счетчик позиции увеличивается, когда к мотору приложено отрицательное напряжение. Измените состояние флага, если положительное вращение мотора уменьшает счетчик позиции. \endrussian */
#define ENGINE_CURRENT_AS_RMS   0x02 	/**< \english Engine current meaning flag. If the flag is unset, then the engine's current value is interpreted as the maximum amplitude value. If the flag is set, then the engine current value is interpreted as the root-mean-square current value (for stepper) or as the current value calculated from the maximum heat dissipation (BLDC). \endenglish \russian Флаг интерпретации значения тока. Если флаг снят, то задаваемое значение тока интерпретируется как максимальная амплитуда тока. Если флаг установлен, то задаваемое значение тока интерпретируется как среднеквадратичное значение тока (для шагового) или как значение тока, посчитанное из максимального тепловыделения (BLDC). \endrussian */
#define ENGINE_MAX_SPEED        0x04 	/**< \english Max speed flag. If it is set, the engine uses the maximum speed achievable with the present engine settings as its nominal speed. \endenglish \russian Флаг максимальной скорости. Если флаг установлен, движение происходит на максимальной скорости. \endrussian */
#define ENGINE_ANTIPLAY         0x08 	/**< \english Play compensation flag. If it is set, the engine makes backlash (play) compensation and reaches the predetermined position accurately at low speed. \endenglish \russian Компенсация люфта. Если флаг установлен, позиционер будет подходить к заданной точке всегда с одной стороны. Например, при подходе слева никаких дополнительных действий не совершается, а при подходе справа позиционер проходит целевую позицию на заданное расстояния и возвращается к ней опять же справа. \endrussian */
#define ENGINE_ACCEL_ON         0x10 	/**< \english Acceleration enable flag. If it set, motion begins with acceleration and ends with deceleration. \endenglish \russian Ускорение. Если флаг установлен, движение происходит с ускорением. \endrussian */
#define ENGINE_LIMIT_VOLT       0x20 	/**< \english Maximum motor voltage limit enable flag (is only used with DC motor). \endenglish \russian Номинальное напряжение мотора. Если флаг установлен, напряжение на моторе ограничивается заданным номинальным значением(используется только с DC двигателем). \endrussian */
#define ENGINE_LIMIT_CURR       0x40 	/**< \english Maximum motor current limit enable flag (is only used with DC motor). \endenglish \russian Номинальный ток мотора. Если флаг установлен, ток через мотор ограничивается заданным номинальным значением(используется только с DC двигателем). \endrussian */
#define ENGINE_LIMIT_RPM        0x80 	/**< \english Maximum motor speed limit enable flag. \endenglish \russian Номинальная частота вращения мотора. Если флаг установлен, частота вращения ограничивается заданным номинальным значением. \endrussian */
//@}


/**
	* @anchor flagset_microstepmode
	* \english
	* @name Flags of microstep mode
	* This is a bit mask for bitwise operations.
	* Specify settings for microstep mode. Used with step motors.
	* Flags returned by query of engine settings. May be combined with bitwise OR
	* \endenglish
	* \russian
	* @name Флаги параметров микрошагового режима
	* Это битовая маска для побитовых операций.
	* Определяют деление шага в микрошаговом режиме. Используются с шаговыми моторами.
	* Возвращаются командой get_engine_settings. Могут быть объединены с помощью логического ИЛИ.
	* \endrussian
	* @see engine_settings_t::flags
	* @see set_engine_settings
	* @see get_engine_settings
	* @see engine_settings_t::MicrostepMode, get_engine_settings, set_engine_settings
	*/
//@{
#define MICROSTEP_MODE_FULL       0x01 	/**< \english Full step mode. \endenglish \russian Полношаговый режим. \endrussian */
#define MICROSTEP_MODE_FRAC_2     0x02 	/**< \english 1/2-step mode. \endenglish \russian Деление шага 1/2. \endrussian */
#define MICROSTEP_MODE_FRAC_4     0x03 	/**< \english 1/4-step mode. \endenglish \russian Деление шага 1/4. \endrussian */
#define MICROSTEP_MODE_FRAC_8     0x04 	/**< \english 1/8-step mode. \endenglish \russian Деление шага 1/8. \endrussian */
#define MICROSTEP_MODE_FRAC_16    0x05 	/**< \english 1/16-step mode. \endenglish \russian Деление шага 1/16. \endrussian */
#define MICROSTEP_MODE_FRAC_32    0x06 	/**< \english 1/32-step mode. \endenglish \russian Деление шага 1/32. \endrussian */
#define MICROSTEP_MODE_FRAC_64    0x07 	/**< \english 1/64-step mode. \endenglish \russian Деление шага 1/64. \endrussian */
#define MICROSTEP_MODE_FRAC_128   0x08 	/**< \english 1/128-step mode. \endenglish \russian Деление шага 1/128. \endrussian */
#define MICROSTEP_MODE_FRAC_256   0x09 	/**< \english 1/256-step mode. \endenglish \russian Деление шага 1/256. \endrussian */
//@}


/**
	* @anchor flagset_enginetype
	* \english
	* @name Flags of engine type
	* This is a bit mask for bitwise operations.
	* Specify motor type.
	* Flags returned by query of engine settings.
	* \endenglish
	* \russian
	* @name Флаги, определяющие тип мотора
	* Это битовая маска для побитовых операций.
	* Определяют тип мотора.
	* Возвращаются командой get_entype_settings.
	* \endrussian
	* @see engine_settings_t::flags
	* @see set_entype_settings
	* @see get_entype_settings
	* @see entype_settings_t::EngineType, get_entype_settings, set_entype_settings
	*/
//@{
#define ENGINE_TYPE_NONE        0x00 	/**< \english A value that shouldn't be used. \endenglish \russian Это значение не нужно использовать. \endrussian */
#define ENGINE_TYPE_DC          0x01 	/**< \english DC motor. \endenglish \russian Мотор постоянного тока. \endrussian */
#define ENGINE_TYPE_2DC         0x02 	/**< \english 2 DC motors. \endenglish \russian Два мотора постоянного тока, что приводит к эмуляции двух контроллеров. \endrussian */
#define ENGINE_TYPE_STEP        0x03 	/**< \english Step motor. \endenglish \russian Шаговый мотор. \endrussian */
#define ENGINE_TYPE_TEST        0x04 	/**< \english Duty cycle are fixed. Used only manufacturer. \endenglish \russian Продолжительность включения фиксирована. Используется только производителем. \endrussian */
#define ENGINE_TYPE_BRUSHLESS   0x05 	/**< \english Brushless motor. \endenglish \russian Бесщеточный мотор. \endrussian */
//@}


/**
	* @anchor flagset_drivertype
	* \english
	* @name Flags of driver type
	* This is a bit mask for bitwise operations.
	* Specify driver type.
	* Flags returned by query of engine settings.
	* \endenglish
	* \russian
	* @name Флаги, определяющие тип силового драйвера
	* Это битовая маска для побитовых операций.
	* Определяют тип силового драйвера.
	* Возвращаются командой get_entype_settings.
	* \endrussian
	* @see engine_settings_t::flags
	* @see set_entype_settings
	* @see get_entype_settings
	* @see entype_settings_t::DriverType, get_entype_settings, set_entype_settings
	*/
//@{
#define DRIVER_TYPE_DISCRETE_FET   0x01 	/**< \english Driver with discrete FET keys. Default option. \endenglish \russian Силовой драйвер на дискретных мосфет-ключах. Используется по умолчанию. \endrussian */
#define DRIVER_TYPE_INTEGRATE      0x02 	/**< \english Driver with integrated IC. \endenglish \russian Силовой драйвер с использованием ключей, интегрированных в микросхему. \endrussian */
#define DRIVER_TYPE_EXTERNAL       0x03 	/**< \english External driver. \endenglish \russian Внешний силовой драйвер. \endrussian */
//@}


/**
	* @anchor flagset_powerflags
	* \english
	* @name Flags of power settings of stepper motor
	* This is a bit mask for bitwise operations.
	* Flags returned by query of engine settings.
	* Specify power settings. Flags returned by query of power settings.
	* \endenglish
	* \russian
	* @name Флаги параметров питания шагового мотора
	* Это битовая маска для побитовых операций.
	* Возвращаются командой get_power_settings.
	* \endrussian
	* @see get_power_settings
	* @see set_power_settings
	* @see power_settings_t::PowerFlags, get_power_settings, set_power_settings
	*/
//@{
#define POWER_REDUCT_ENABLED   0x01 	/**< \english Current reduction is enabled after CurrReductDelay if this flag is set. \endenglish \russian Если флаг установлен, уменьшить ток по прошествии CurrReductDelay. Иначе - не уменьшать. \endrussian */
#define POWER_OFF_ENABLED      0x02 	/**< \english Power off is enabled after PowerOffDelay if this flag is set. \endenglish \russian Если флаг установлен, снять напряжение с обмоток по прошествии PowerOffDelay. Иначе - не снимать. \endrussian */
#define POWER_SMOOTH_CURRENT   0x04 	/**< \english Current ramp-up/down are performed smoothly during current_set_time if this flag is set. \endenglish \russian Если установлен, то запитывание обмоток, снятие питания или снижение/повышение тока происходят плавно со скоростью CurrentSetTime, а только потом выполняется та задача, которая вызвала это плавное изменение. \endrussian */
//@}


/**
	* @anchor flagset_secureflags
	* \english
	* @name Flags of secure settings
	* This is a bit mask for bitwise operations.
	* Flags returned by query of engine settings.
	* Specify secure settings. Flags returned by query of secure settings.
	* \endenglish
	* \russian
	* @name Флаги критических параметров.
	* Это битовая маска для побитовых операций.
	* Возвращаются командой get_secure_settings.
	* \endrussian
	* @see get_secure_settings
	* @see set_secure_settings
	* @see secure_settings_t::Flags, get_secure_settings, set_secure_settings
	*/
//@{
#define ALARM_ON_DRIVER_OVERHEATING    0x01 	/**< \english If this flag is set, enter the alarm state on the driver overheat signal. \endenglish \russian Если флаг установлен, то войти в состояние Alarm при получении сигнала подступающего перегрева с драйвера. Иначе - игнорировать подступающий перегрев с драйвера. \endrussian */
#define LOW_UPWR_PROTECTION            0x02 	/**< \english If this flag is set, turn off the motor when the voltage is lower than LowUpwrOff. \endenglish \russian Если установлен, то выключать силовую часть при напряжении меньшем LowUpwrOff. \endrussian */
#define H_BRIDGE_ALERT                 0x04 	/**< \english If this flag is set then turn off the power unit with a signal problem in one of the transistor bridge. \endenglish \russian Если установлен, то выключать силовую часть при сигнале неполадки в одном из транзисторных мостов.\endrussian */
#define ALARM_ON_BORDERS_SWAP_MISSET   0x08 	/**< \english If this flag is set, enter Alarm state on borders swap misset \endenglish \russian Если флаг установлен, то войти в состояние Alarm при получении сигнала c противоположного концевого выключателя.\endrussian */
#define ALARM_FLAGS_STICKING           0x10 	/**< \english If this flag is set, only a STOP command can turn all alarms to 0 \endenglish \russian Если флаг установлен, то только по команде STOP возможен сброс всех флагов ALARM.\endrussian */
#define USB_BREAK_RECONNECT            0x20 	/**< \english Deprecated. If this flag is set, the USB brake reconnect module will be enabled \endenglish \russian Устарело. Если флаг установлен, то будет включен блок перезагрузки USB при поломке связи.\endrussian */
#define ALARM_WINDING_MISMATCH         0x40 	/**< \english If this flag is set, enter Alarm state when windings mismatch \endenglish \russian Если флаг установлен, то войти в состояние Alarm при получении сигнала рассогласования обмоток \endrussian */
#define ALARM_ENGINE_RESPONSE          0x80 	/**< \english If this flag is set, enter the Alarm state on response of the engine control action \endenglish \russian Если флаг установлен, то войти в состояние Alarm при получении сигнала ошибки реакции двигателя на управляющее воздействие  \endrussian */
//@}


/**
	* @anchor flagset_positionflags
	* \english
	* @name Position setting flags
	* This is a bit mask for bitwise operations.
	* Flags used in setting position.
	* \endenglish
	* \russian
	* @name Флаги установки положения
	* Это битовая маска для побитовых операций.
	* Возвращаются командой get_position.
	* \endrussian
	* @see get_position
	* @see set_position
	* @see set_position_t::PosFlags, set_position
	*/
//@{
#define SETPOS_IGNORE_POSITION   0x01 	/**< \english Will not reload position in steps/microsteps if this flag is set. \endenglish \russian Если установлен, то позиция в шагах и микрошагах не обновляется. \endrussian */
#define SETPOS_IGNORE_ENCODER    0x02 	/**< \english Will not reload encoder state if this flag is set. \endenglish \russian Если установлен, то счётчик энкодера не обновляется. \endrussian */
//@}


/**
	* @anchor flagset_feedbacktype
	* \english
	* @name Feedback type.
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Тип обратной связи.
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see set_feedback_settings
	* @see get_feedback_settings
	* @see feedback_settings_t::FeedbackType, get_feedback_settings, set_feedback_settings
	*/
//@{
#define FEEDBACK_ENCODER            0x01 	/**< \english Feedback by encoder. \endenglish \russian Обратная связь с помощью энкодера. \endrussian */
#define FEEDBACK_EMF                0x04 	/**< \english Feedback by EMF. \endenglish \russian Обратная связь по ЭДС. \endrussian */
#define FEEDBACK_NONE               0x05 	/**< \english Feedback is absent. \endenglish \russian Обратная связь отсутствует. \endrussian */
#define FEEDBACK_ENCODER_MEDIATED   0x06 	/**< \english Feedback by encoder mediated by mechanical transmission (for example leadscrew). \endenglish \russian Обратная связь по энкодеру, опосредованному относительно двигателя механической передачей (например, винтовой передачей). \endrussian */
//@}


/**
	* @anchor flagset_feedbackflags
	* \english
	* @name Describes feedback flags.
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги обратной связи.
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see set_feedback_settings
	* @see get_feedback_settings
	* @see feedback_settings_t::FeedbackFlags, get_feedback_settings, set_feedback_settings
	*/
//@{
#define FEEDBACK_ENC_REVERSE             0x01 	/**< \english Reverse count of encoder. \endenglish \russian Обратный счет у энкодера. \endrussian */
#define FEEDBACK_ENC_ADAPTIVE_HOLDING    0x02 	/**< \english Enables the adaptive holding algorithm. \endenglish \russian Включает алгоритм адаптивного удержания. \endrussian */
#define FEEDBACK_ENC_TYPE_BITS           0xC0 	/**< \english Bits of the encoder type. \endenglish \russian Биты, отвечающие за тип энкодера. \endrussian */
#define FEEDBACK_ENC_TYPE_AUTO           0x00 	/**< \english Auto detect encoder type. \endenglish \russian Определяет тип энкодера автоматически. \endrussian */
#define FEEDBACK_ENC_TYPE_SINGLE_ENDED   0x40 	/**< \english Single-ended encoder. \endenglish \russian Недифференциальный энкодер. \endrussian */
#define FEEDBACK_ENC_TYPE_DIFFERENTIAL   0x80 	/**< \english Differential encoder. \endenglish \russian Дифференциальный энкодер. \endrussian */
//@}


/**
	* @anchor flagset_syncinflags
	* \english
	* @name Flags for synchronization input setup
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги настроек синхронизации входа
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see sync_in_settings_t::SyncInFlags, get_sync_in_settings, set_sync_in_settings
	*/
//@{
#define SYNCIN_ENABLED        0x01 	/**< \english Synchronization in mode is enabled if this flag is set. \endenglish \russian Включение необходимости импульса синхронизации для начала движения. \endrussian */
#define SYNCIN_INVERT         0x02 	/**< \english Trigger on falling edge if flag is set, on rising edge otherwise. \endenglish \russian Если установлен - срабатывает по переходу из 1 в 0. Иначе - из 0 в 1. \endrussian */
#define SYNCIN_GOTOPOSITION   0x04 	/**< \english The engine is going to the position specified in Position and uPosition if this flag is set. And it is shifting on the Position and uPosition if this flag is unset \endenglish \russian Если флаг установлен, то двигатель смещается к позиции, установленной в Position и uPosition, иначе двигатель смещается на Position и uPosition \endrussian */
//@}


/**
	* @anchor flagset_syncoutflags
	* \english
	* @name Flags of synchronization output
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги настроек синхронизации выхода
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see sync_out_settings_t::SyncOutFlags, get_sync_out_settings, set_sync_out_settings
	*/
//@{
#define SYNCOUT_ENABLED    0x01 	/**< \english The synchronization out pin follows the synchronization logic if the flag is set. Otherwise, it is governed by the SYNCOUT_STATE flag. \endenglish \russian Синхронизация выхода работает согласно настройкам, если флаг установлен. В ином случае значение выхода фиксировано и подчиняется SYNCOUT_STATE. \endrussian */
#define SYNCOUT_STATE      0x02 	/**< \english When the output state is fixed by the negative SYNCOUT_ENABLED flag, the pin state is in accordance with this flag state. \endenglish \russian Когда значение выхода управляется напрямую (см. флаг SYNCOUT_ENABLED), значение на выходе соответствует значению этого флага. \endrussian */
#define SYNCOUT_INVERT     0x04 	/**< \english The low level is active if the flag is set. Otherwise,  the high level is active. \endenglish \russian Нулевой логический уровень является активным, если флаг установлен, а единичный - если флаг сброшен. \endrussian */
#define SYNCOUT_IN_STEPS   0x08 	/**< \english Use motor steps or encoder pulses instead of milliseconds for output pulse generation if the flag is set. \endenglish \russian Если флаг установлен использовать шаги/импульсы энкодера для выходных импульсов синхронизации вместо миллисекунд. \endrussian */
#define SYNCOUT_ONSTART    0x10 	/**< \english Generate a synchronization pulse when movement starts. \endenglish \russian Генерация синхронизирующего импульса при начале движения. \endrussian */
#define SYNCOUT_ONSTOP     0x20 	/**< \english Generate a synchronization pulse when movement stops. \endenglish \russian Генерация синхронизирующего импульса при остановке. \endrussian */
#define SYNCOUT_ONPERIOD   0x40 	/**< \english Generate a synchronization pulse every SyncOutPeriod encoder pulses. \endenglish \russian Выдает импульс синхронизации после прохождения SyncOutPeriod отсчётов. \endrussian */
//@}


/**
	* @anchor flagset_extiosetupflags
	* \english
	* @name External IO setup flags
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги настройки работы внешнего ввода/вывода
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see get_extio_settings
	* @see set_extio_settings
	* @see extio_settings_t::EXTIOSetupFlags, get_extio_settings, set_extio_settings
	*/
//@{
#define EXTIO_SETUP_OUTPUT   0x01 	/**< \english EXTIO works as output if the flag is set, works as input otherwise. \endenglish \russian Если флаг установлен, то ножка в состоянии вывода, иначе - ввода. \endrussian */
#define EXTIO_SETUP_INVERT   0x02 	/**< \english Interpret EXTIO state inverted if the flag is set. A falling front is treated as an input event and a low logic level as an active state. \endenglish \russian Если флаг установлен, то нули считаются активным состоянием выхода, а спадающие фронты как момент подачи входного сигнала. \endrussian */
//@}


/**
	* @anchor flagset_extiomodeflags
	* \english
	* @name External IO mode flags
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги настройки режимов внешнего ввода/вывода
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see extio_settings_t::extio_mode_flags
	* @see get_extio_settings
	* @see set_extio_settings
	* @see extio_settings_t::EXTIOModeFlags, get_extio_settings, set_extio_settings
	*/
//@{
#define EXTIO_SETUP_MODE_IN_BITS        0x0F 	/**< \english Bits of the behavior selector when the signal on input goes to the active state. \endenglish \russian Биты, отвечающие за поведение при переходе сигнала в активное состояние. \endrussian */
#define EXTIO_SETUP_MODE_IN_NOP         0x00 	/**< \english Do nothing. \endenglish \russian Ничего не делать. \endrussian */
#define EXTIO_SETUP_MODE_IN_STOP        0x01 	/**< \english Issue STOP command, ceasing the engine movement. \endenglish \russian По переднему фронту входного сигнала делается остановка двигателя (эквивалент команды STOP). \endrussian */
#define EXTIO_SETUP_MODE_IN_PWOF        0x02 	/**< \english Issue PWOF command, powering off all engine windings. \endenglish \russian Выполняет команду PWOF, обесточивая обмотки двигателя. \endrussian */
#define EXTIO_SETUP_MODE_IN_MOVR        0x03 	/**< \english Issue MOVR command with last used settings. \endenglish \russian Выполняется команда MOVR с последними настройками. \endrussian */
#define EXTIO_SETUP_MODE_IN_HOME        0x04 	/**< \english Issue HOME command. \endenglish \russian Выполняется команда HOME. \endrussian */
#define EXTIO_SETUP_MODE_IN_ALARM       0x05 	/**< \english Set Alarm when the signal goes to the active state. \endenglish \russian Войти в состояние ALARM при переходе сигнала в активное состояние. \endrussian */
#define EXTIO_SETUP_MODE_OUT_BITS       0xF0 	/**< \english Bits of the output behavior selection. \endenglish \russian Биты выбора поведения на выходе. \endrussian */
#define EXTIO_SETUP_MODE_OUT_OFF        0x00 	/**< \english EXTIO pin always set in inactive state. \endenglish \russian Ножка всегда в неактивном состоянии. \endrussian */
#define EXTIO_SETUP_MODE_OUT_ON         0x10 	/**< \english EXTIO pin always set in active state. \endenglish \russian Ножка всегда в активном состоянии. \endrussian */
#define EXTIO_SETUP_MODE_OUT_MOVING     0x20 	/**< \english EXTIO pin stays active during moving state. \endenglish \russian Ножка находится в активном состоянии при движении. \endrussian */
#define EXTIO_SETUP_MODE_OUT_ALARM      0x30 	/**< \english EXTIO pin stays active during the alarm state. \endenglish \russian Ножка находится в активном состоянии при нахождении в состоянии ALARM. \endrussian */
#define EXTIO_SETUP_MODE_OUT_MOTOR_ON   0x40 	/**< \english EXTIO pin stays active when windings are powered. \endenglish \russian Ножка находится в активном состоянии при подаче питания на обмотки. \endrussian */
//@}


/**
	* @anchor flagset_borderflags
	* \english
	* @name Border flags
	* This is a bit mask for bitwise operations.
	* Specify types of borders and motor behavior on borders.
	* May be combined with bitwise OR.
	* \endenglish
	* \russian
	* @name Флаги границ
	* Это битовая маска для побитовых операций.
	* Типы границ и поведение позиционера на границах.
	* Могут быть объединены с помощью побитового ИЛИ.
	* \endrussian
	* @see get_edges_settings
	* @see set_edges_settings
	* @see edges_settings_t::BorderFlags, get_edges_settings, set_edges_settings
	*/
//@{
#define BORDER_IS_ENCODER               0x01 	/**< \english Borders are fixed by predetermined encoder values, if set; borders are placed on limit switches, if not set. \endenglish \russian Если флаг установлен, границы определяются предустановленными точками на шкале позиции. Если флаг сброшен, границы определяются концевыми выключателями. \endrussian */
#define BORDER_STOP_LEFT                0x02 	/**< \english The motor should stop on the left border. \endenglish \russian Если флаг установлен, мотор останавливается при достижении левой границы. \endrussian */
#define BORDER_STOP_RIGHT               0x04 	/**< \english Motor should stop on right border. \endenglish \russian Если флаг установлен, мотор останавливается при достижении правой границы. \endrussian */
#define BORDERS_SWAP_MISSET_DETECTION   0x08 	/**< \english Motor should stop on both borders. Need to save the motor when wrong border settings is set\endenglish \russian Если флаг установлен, мотор останавливается по достижении любой из границ. Нужен для предотвращения поломки двигателя при неправильных настройках концевых выключателей \endrussian */
//@}


/**
	* @anchor flagset_enderflags
	* \english
	* @name Limit switches flags
	* This is a bit mask for bitwise operations.
	* Specify electrical behavior of limit switches like order and pulled positions.
	* May be combined with bitwise OR.
	* \endenglish
	* \russian
	* @name Флаги концевых выключателей
	* Это битовая маска для побитовых операций.
	* Определяют направление и состояние границ.
	* Могут быть объединены с помощью побитового ИЛИ.
	* \endrussian
	* @see get_edges_settings
	* @see set_edges_settings
	* @see edges_settings_t::EnderFlags, get_edges_settings, set_edges_settings
	*/
//@{
#define ENDER_SWAP             0x01 	/**< \english First limit switch on the right side, if set; otherwise on the left side. \endenglish \russian Если флаг установлен, первый концевой выключатель находится справа; иначе - слева. \endrussian */
#define ENDER_SW1_ACTIVE_LOW   0x02 	/**< \english 1 - Limit switch connected to pin SW1 is triggered by a low level on pin. \endenglish \russian 1 - Концевой переключатель, подключенный к ножке SW1, считается сработавшим по низкому уровню на контакте. \endrussian */
#define ENDER_SW2_ACTIVE_LOW   0x04 	/**< \english 1 - Limit switch connected to pin SW2 is triggered by a low level on pin. \endenglish \russian 1 - Концевой переключатель, подключенный к ножке SW2, считается сработавшим по низкому уровню на контакте. \endrussian */
//@}


/**
	* @anchor flagset_brakeflags
	* \english
	* @name Brake settings flags
	* This is a bit mask for bitwise operations.
	* Specify behavior of brake.
	* May be combined with bitwise OR.
	* \endenglish
	* \russian
	* @name Флаги настроек тормоза
	* Это битовая маска для побитовых операций.
	* Определяют поведение тормоза.
	* Могут быть объединены с помощью побитового ИЛИ.
	* \endrussian
	* @see get_brake_settings
	* @see set_brake_settings
	* @see brake_settings_t::BrakeFlags, get_brake_settings, set_brake_settings
	*/
//@{
#define BRAKE_ENABLED      0x01 	/**< \english Brake control is enabled if this flag is set. \endenglish \russian Управление тормозом включено, если флаг установлен. \endrussian */
#define BRAKE_ENG_PWROFF   0x02 	/**< \english Brake turns the stepper motor power off if this flag is set. \endenglish \russian Тормоз отключает питание шагового мотора, если флаг установлен. \endrussian */
//@}


/**
	* @anchor flagset_controlflags
	* \english
	* @name Control flags
	* This is a bit mask for bitwise operations.
	* Specify motor control settings by joystick or buttons.
	* May be combined with bitwise OR.
	* \endenglish
	* \russian
	* @name Флаги управления
	* Это битовая маска для побитовых операций.
	* Определяют параметры управления мотором с помощью джойстика или кнопок.
	* Могут быть объединены с помощью побитового ИЛИ.
	* \endrussian
	* @see get_control_settings
	* @see set_control_settings
	* @see control_settings_t::Flags, get_control_settings, set_control_settings
	*/
//@{
#define CONTROL_MODE_BITS               0x03 	/**< \english Bits to control the engine by joystick or buttons. \endenglish \russian Биты управления мотором с помощью джойстика или кнопок влево/вправо. \endrussian */
#define CONTROL_MODE_OFF                0x00 	/**< \english Control is disabled. \endenglish \russian Управление отключено. \endrussian */
#define CONTROL_MODE_JOY                0x01 	/**< \english Control by joystick. \endenglish \russian Управление с помощью джойстика. \endrussian */
#define CONTROL_MODE_LR                 0x02 	/**< \english Control by left/right buttons. \endenglish \russian Управление с помощью кнопок влево/вправо. \endrussian */
#define CONTROL_BTN_LEFT_PUSHED_OPEN    0x04 	/**< \english Pushed left button corresponds to the open contact if this flag is set. \endenglish \russian Нажатая левая кнопка соответствует открытому контакту, если этот флаг установлен. \endrussian */
#define CONTROL_BTN_RIGHT_PUSHED_OPEN   0x08 	/**< \english Pushed right button corresponds to open contact if this flag is set. \endenglish \russian Нажатая правая кнопка соответствует открытому контакту, если этот флаг установлен. \endrussian */
//@}


/**
	* @anchor flagset_joyflags
	* \english
	* @name Joystick flags
	* This is a bit mask for bitwise operations.
	* Control joystick states.
	* \endenglish
	* \russian
	* @name Флаги джойстика
	* Это битовая маска для побитовых операций.
	* Управляют состояниями джойстика.
	* \endrussian
	* @see set_joystick_settings
	* @see get_joystick_settings
	* @see joystick_settings_t::JoyFlags, get_joystick_settings, set_joystick_settings
	*/
//@{
#define JOY_REVERSE   0x01 	/**< \english Joystick action is reversed. The joystick deviation to the upper values corresponds to negative speed and vice versa. \endenglish \russian Реверс воздействия джойстика. Отклонение джойстика к большим значениям приводит к отрицательной скорости и наоборот. \endrussian */
//@}


/**
	* @anchor flagset_ctpflags
	* \english
	* @name Position control flags
	* This is a bit mask for bitwise operations.
	* Specify control position settings.
	* May be combined with bitwise OR.
	* \endenglish
	* \russian
	* @name Флаги контроля позиции
	* Это битовая маска для побитовых операций.
	* Определяют настройки контроля позиции.
	* Могут быть объединены с помощью побитового ИЛИ.
	* \endrussian
	* @see get_ctp_settings
	* @see set_ctp_settings
	* @see ctp_settings_t::CTPFlags, get_ctp_settings, set_ctp_settings
	*/
//@{
#define CTP_ENABLED            0x01 	/**< \english The position control is enabled if the flag is set. \endenglish \russian Контроль позиции включен, если флаг установлен. \endrussian */
#define CTP_BASE               0x02 	/**< \english The position control is based on the revolution sensor if this flag is set; otherwise, it is based on the encoder. \endenglish \russian Управление положением основано на датчике вращения, если установлен этот флаг; в противном случае - на энкодере. \endrussian */
#define CTP_ALARM_ON_ERROR     0x04 	/**< \english Set ALARM on mismatch if the flag is set. \endenglish \russian Войти в состояние ALARM при расхождении позиции, если флаг установлен. \endrussian */
#define REV_SENS_INV           0x08 	/**< \english Typically, the sensor is active when it is at 0, and inversion makes active at 1. That is, if you do not invert, it is normal logic - 0 is the activation. \endenglish \russian Сенсор считается активным, когда на нём 0, инвертирование делает активным уровень 1. То есть если не инвертировать, то действует обычная логика - 0 это срабатывание/активация/активное состояние. \endrussian */
#define CTP_ERROR_CORRECTION   0x10 	/**< \english Correct errors that appear when slippage occurs if the flag is set. It works only with the encoder. Incompatible with the flag CTP_ALARM_ON_ERROR. \endenglish \russian Корректировать ошибки, возникающие при проскальзывании, если флаг установлен. Работает только с энкодером. Несовместимо с флагом CTP_ALARM_ON_ERROR.\endrussian */
//@}


/**
	* @anchor flagset_homeflags
	* \english
	* @name Home settings flags
	* This is a bit mask for bitwise operations.
	* Specify home command behavior.
	* May be combined with bitwise OR.
	* \endenglish
	* \russian
	* @name Флаги настроек команды home
	* Это битовая маска для побитовых операций.
	* Определяют поведение для команды home.
	* Могут быть объединены с помощью побитового ИЛИ.
	* \endrussian
	* @see get_home_settings
	* @see set_home_settings
	* @see command_home
	* @see home_settings_t::HomeFlags, get_home_settings, set_home_settings
	*/
//@{
#define HOME_DIR_FIRST          0x001 	/**< \english The flag defines the direction of the 1st motion after execution of the home command. The direction is to the right if the flag is set, and to the left otherwise. \endenglish \russian Определяет направление первоначального движения мотора после поступления команды HOME. Если флаг установлен - вправо; иначе - влево. \endrussian */
#define HOME_DIR_SECOND         0x002 	/**< \english The flag defines the direction of the 2nd motion. The direction is to the right if the flag is set, and to the left otherwise. \endenglish \russian Определяет направление второго движения мотора. Если флаг установлен - вправо; иначе - влево. \endrussian */
#define HOME_MV_SEC_EN          0x004 	/**< \english Use the second phase of calibration to the home position, if set; otherwise the second phase is skipped. \endenglish \russian Если флаг установлен, реализуется второй этап доводки в домашнюю позицию; иначе - этап пропускается. \endrussian */
#define HOME_HALF_MV            0x008 	/**< \english If the flag is set, the stop signals are ignored during the first half-turn of the second movement. \endenglish \russian Если флаг установлен, в начале второго движения первые пол оборота сигналы завершения движения игнорируются. \endrussian */
#define HOME_STOP_FIRST_BITS    0x030 	/**< \english Bits of the first stop selector. \endenglish \russian Биты, отвечающие за выбор сигнала завершения первого движения. \endrussian */
#define HOME_STOP_FIRST_REV     0x010 	/**< \english First motion stops by  revolution sensor. \endenglish \russian Первое движение завершается по сигналу с Revolution sensor. \endrussian */
#define HOME_STOP_FIRST_SYN     0x020 	/**< \english First motion stops by synchronization input. \endenglish \russian Первое движение завершается по сигналу со входа синхронизации. \endrussian */
#define HOME_STOP_FIRST_LIM     0x030 	/**< \english First motion stops by limit switch. \endenglish \russian Первое движение завершается по сигналу с концевого переключателя. \endrussian */
#define HOME_STOP_SECOND_BITS   0x0C0 	/**< \english Bits of the second stop selector. \endenglish \russian Биты, отвечающие за выбор сигнала завершения второго движения. \endrussian */
#define HOME_STOP_SECOND_REV    0x040 	/**< \english Second motion stops by  revolution sensor. \endenglish \russian Второе движение завершается по сигналу с Revolution sensor. \endrussian */
#define HOME_STOP_SECOND_SYN    0x080 	/**< \english Second motion stops by synchronization input. \endenglish \russian Второе движение завершается по сигналу со входа синхронизации. \endrussian */
#define HOME_STOP_SECOND_LIM    0x0C0 	/**< \english Second motion stops by limit switch. \endenglish \russian Второе движение завершается по сигналу с концевого переключателя. \endrussian */
#define HOME_USE_FAST           0x100 	/**< \english Use the fast algorithm of calibration to the home position, if set; otherwise the traditional algorithm. \endenglish \russian Если флаг установлен, используется быстрый поиск домашней позиции; иначе - традиционный. \endrussian */
//@}


/**
	* @anchor flagset_uartsetupflags
	* \english
	* @name UART parity flags
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги настроек четности команды UART
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see uart_settings_t::UARTSetupFlags, get_uart_settings, set_uart_settings
	*/
//@{
#define UART_PARITY_BITS        0x03 	/**< \english Bits of the parity. \endenglish \russian Биты, отвечающие за выбор четности. \endrussian */
#define UART_PARITY_BIT_EVEN    0x00 	/**< \english Parity bit 1, if  even \endenglish \russian Бит 1, если четный \endrussian */
#define UART_PARITY_BIT_ODD     0x01 	/**< \english Parity bit 1, if  odd \endenglish \russian Бит 1, если нечетный \endrussian */
#define UART_PARITY_BIT_SPACE   0x02 	/**< \english Parity bit always 0 \endenglish \russian Бит четности всегда 0 \endrussian */
#define UART_PARITY_BIT_MARK    0x03 	/**< \english Parity bit always 1 \endenglish \russian Бит четности всегда 1 \endrussian */
#define UART_PARITY_BIT_USE     0x04 	/**< \english None parity \endenglish \russian Бит чётности не используется, если "0"; бит четности используется, если "1" \endrussian */
#define UART_STOP_BIT           0x08 	/**< \english If set - one stop bit, else two stop bit \endenglish \russian Если установлен, один стоповый бит; иначе - 2 стоповых бита \endrussian */
//@}


/** 
	* @anchor flagset_motortypeflags
 	* \english
	* @name Motor Type flags
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги типа двигателя
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see motor_settings_t::MotorType, get_motor_settings, set_motor_settings
	*/
//@{
#define MOTOR_TYPE_UNKNOWN   0x00 	/**< \english Unknown type of engine \endenglish \russian Неизвестный двигатель \endrussian */
#define MOTOR_TYPE_STEP      0x01 	/**< \english Step engine \endenglish \russian Шаговый двигатель \endrussian */
#define MOTOR_TYPE_DC        0x02 	/**< \english DC engine \endenglish \russian DC двигатель \endrussian */
#define MOTOR_TYPE_BLDC      0x03 	/**< \english BLDC engine \endenglish \russian BLDC двигатель \endrussian */
//@}


/** 
	* @anchor flagset_encodersettingsflags
 	* \english
	* @name Encoder settings flags
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги настроек энкодера
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see encoder_settings_t::EncoderSettings, get_encoder_settings, set_encoder_settings
	*/
//@{
#define ENCSET_DIFFERENTIAL_OUTPUT            0x001 	/**< \english If the flag is set, the encoder has differential output, otherwise single-ended output \endenglish \russian Если флаг установлен, то энкодер имеет дифференциальный выход, иначе - несимметричный выход \endrussian */
#define ENCSET_PUSHPULL_OUTPUT                0x004 	/**< \english If the flag is set the encoder has push-pull output, otherwise open drain output \endenglish \russian Если флаг установлен, то энкодер имеет двухтактный выход, иначе - выход с открытым коллектором \endrussian */
#define ENCSET_INDEXCHANNEL_PRESENT           0x010 	/**< \english If the flag is set, the encoder has an extra indexed channel \endenglish \russian Если флаг установлен, то энкодер имеет дополнительный индексный канал, иначе - он отсутствует \endrussian */
#define ENCSET_REVOLUTIONSENSOR_PRESENT       0x040 	/**< \english If the flag is set, the encoder has the revolution sensor \endenglish \russian Если флаг установлен, то энкодер имеет датчик оборотов, иначе - он отсутствует \endrussian */
#define ENCSET_REVOLUTIONSENSOR_ACTIVE_HIGH   0x100 	/**< \english If the flag is set, the revolution sensor's active state is high logic state; otherwise, the active state is low logic state \endenglish \russian Если флаг установлен, то активное состояние датчика оборотов соответствует логической 1, иначе - логическому 0 \endrussian */
//@}


/** 
	* @anchor flagset_mbsettingsflags
 	* \english
	* @name Magnetic brake settings flags
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги настроек энкодера
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see accessories_settings_t::MBSettings, get_accessories_settings, set_accessories_settings
	*/
//@{
#define MB_AVAILABLE      0x01 	/**< \english If the flag is set, the magnetic brake is available \endenglish \russian Если флаг установлен, то магнитный тормоз доступен \endrussian */
#define MB_POWERED_HOLD   0x02 	/**< \english If this flag is set, the magnetic brake is on when powered \endenglish \russian Если флаг установлен, то магнитный тормоз находится в режиме удержания (активен) при подаче питания \endrussian */
//@}


/** 
	* @anchor flagset_tssettingsflags
 	* \english
	* @name Temperature sensor settings flags
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги настроек температурного датчика
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see accessories_settings_t::TSSettings, get_accessories_settings, set_accessories_settings
	*/
//@{
#define TS_TYPE_BITS            0x07 	/**< \english Bits of the temperature sensor type \endenglish \russian Биты, отвечающие за тип температурного датчика. \endrussian */
#define TS_TYPE_UNKNOWN         0x00 	/**< \english Unknown type of sensor \endenglish \russian Неизвестный сенсор \endrussian */
#define TS_TYPE_THERMOCOUPLE    0x01 	/**< \english Thermocouple \endenglish \russian Термопара \endrussian */
#define TS_TYPE_SEMICONDUCTOR   0x02 	/**< \english The semiconductor temperature sensor \endenglish \russian Полупроводниковый температурный датчик \endrussian */
#define TS_AVAILABLE            0x08 	/**< \english If the flag is set, the temperature sensor is available \endenglish \russian Если флаг установлен, то датчик температуры доступен \endrussian */
//@}


/** 
	* @anchor flagset_lsflags
 	* \english
	* @name Temperature sensor settings flags
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги настроек температурного датчика
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see accessories_settings_t::LimitSwitchesSettings, get_accessories_settings, set_accessories_settings
	*/
//@{
#define LS_ON_SW1_AVAILABLE   0x01 	/**< \english If the flag is set, the limit switch connected to pin SW1 is available \endenglish \russian Если флаг установлен, то концевой переключатель, подключенный к ножке SW1, доступен \endrussian */
#define LS_ON_SW2_AVAILABLE   0x02 	/**< \english If the flag is set, the limit switch connected to pin SW2 is available \endenglish \russian Если флаг установлен, то концевой переключатель, подключенный к ножке SW2, доступен \endrussian */
#define LS_SW1_ACTIVE_LOW     0x04 	/**< \english If the flag is set, the limit switch connected to pin SW1 is triggered by a low level on the pin \endenglish \russian Если флаг установлен, то концевой переключатель, подключенный к ножке SW1, считается сработавшим по низкому уровню на контакте \endrussian */
#define LS_SW2_ACTIVE_LOW     0x08 	/**< \english If the flag is set, the limit switch connected to pin SW2 is triggered by a low level on pin \endenglish \russian Если флаг установлен, то концевой переключатель, подключенный к ножке SW2, считается сработавшим по низкому уровню на контакте \endrussian */
#define LS_SHORTED            0x10 	/**< \english If the flag is set, the limit switches are shorted \endenglish \russian Если флаг установлен, то концевые переключатели замкнуты. \endrussian */
//@}


/**
	* @anchor flagset_backemfflags
	* \english
	* @name Flags of auto-detection of characteristics of windings of the engine.
	* This is a bit mask for bitwise operations.
	* \endenglish
	* \russian
	* @name Флаги автоопределения характеристик обмоток двигателя.
	* Это битовая маска для побитовых операций.
	* \endrussian
	* @see set_emf_settings
	* @see get_emf_settings
	* @see emf_settings_t::BackEMFFlags, get_emf_settings, set_emf_settings
	*/
//@{
#define BACK_EMF_INDUCTANCE_AUTO   0x01 	/**< \english Flag of auto-detection of inductance of windings of the engine. \endenglish \russian Флаг автоопределения индуктивности обмоток двигателя. \endrussian */
#define BACK_EMF_RESISTANCE_AUTO   0x02 	/**< \english Flag of auto-detection of resistance of windings of the engine. \endenglish \russian Флаг автоопределения сопротивления обмоток двигателя. \endrussian */
#define BACK_EMF_KM_AUTO           0x04 	/**< \english Flag of auto-detection of electromechanical coefficient of the engine. \endenglish \russian Флаг автоопределения электромеханического коэффициента двигателя. \endrussian */
//@}


/** 
	* \english
	* Feedback settings.
	* This structure contains feedback settings.
	* \endenglish
	* \russian
	* Настройки обратной связи.
	* Эта структура содержит настройки обратной связи.
	* \endrussian
	* @see get_feedback_settings, set_feedback_settings
	*/
	typedef struct
	{
		unsigned int IPS;	/**< \english The number of encoder counts per shaft revolution. Range: 1..655535. The field is obsolete, it is recommended to write 0 to IPS and use the extended CountsPerTurn field. You may need to update the controller firmware to the latest version. \endenglish \russian Количество отсчётов энкодера на оборот вала. Диапазон: 1..65535. Поле устарело, рекомендуется записывать 0 в IPS и использовать расширенное поле CountsPerTurn. Может потребоваться обновление микропрограммы контроллера до последней версии. \endrussian */
		unsigned int FeedbackType;	/**< \english \ref flagset_feedbacktype "Feedback type". \endenglish \russian \ref flagset_feedbacktype "Тип обратной связи". \endrussian */
		unsigned int FeedbackFlags;	/**< \english \ref flagset_feedbackflags "Describes feedback flags". \endenglish \russian \ref flagset_feedbackflags "Флаги обратной связи". \endrussian */
		unsigned int CountsPerTurn;	/**< \english The number of encoder counts per shaft revolution. Range: 1..4294967295. To use the CountsPerTurn field, write 0 in the IPS field, otherwise the value from the IPS field will be used. \endenglish \russian Количество отсчётов энкодера на оборот вала. Диапазон: 1..4294967295. Для использования поля CountsPerTurn нужно записать 0 в поле IPS, иначе будет использоваться значение из поля IPS. \endrussian */
	} feedback_settings_t;

/** 
	* \english
	* Position calibration settings.
	* This structure contains settings used in position calibration. It specify behavior of calibration procedure.
	* \endenglish
	* \russian
	* Настройки калибровки позиции.
	* Эта структура содержит настройки, использующиеся при калибровке позиции.
	* \endrussian
	* @see get_home_settings
	* @see set_home_settings
	* @see command_home
	* @see get_home_settings, set_home_settings
	*/
	typedef struct
	{
		unsigned int FastHome;	/**< \english Speed used for first motion (full steps). Range: 0..100000. \endenglish \russian Скорость первого движения (в полных шагах). Диапазон: 0..100000 \endrussian */
		unsigned int uFastHome;	/**< \english Fractional part of the speed for first motion, microsteps. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). \endenglish \russian Дробная часть скорости первого движения в микрошагах (используется только с шаговым двигателем). Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
		unsigned int SlowHome;	/**< \english Speed used for second motion (full steps). Range: 0..100000. \endenglish \russian Скорость второго движения (в полных шагах). Диапазон: 0..100000. \endrussian */
		unsigned int uSlowHome;	/**< \english Part of the speed for second motion, microsteps. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). \endenglish \russian Дробная часть скорости второго движения в микрошагах (используется только с шаговым двигателем). Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
		int HomeDelta;	/**< \english Distance from break point (full steps). \endenglish \russian Расстояние отхода от точки останова (в полных шагах). \endrussian */
		int uHomeDelta;	/**< \english Fractional part of the delta distance, microsteps. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). \endenglish \russian Дробная часть расстояния отхода от точки останова в микрошагах (используется только с шаговым двигателем). Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
		unsigned int HomeFlags;	/**< \english \ref flagset_homeflags "Home settings flags". \endenglish \russian \ref flagset_homeflags "Флаги настроек команды home". \endrussian */
	} home_settings_t;

/** 
	* \english
	* Position calibration settings which use user units.
	* This structure contains settings used in position calibrating.
	* It specifies behavior of calibrating position.
	* \endenglish
	* \russian
	* Настройки калибровки позиции с использованием пользовательских единиц.
	* Эта структура содержит настройки, использующиеся при калибровке позиции.
	* \endrussian
	* @see get_home_settings_calb
	* @see set_home_settings_calb
	* @see command_home
	* @see get_home_settings, set_home_settings
	*/
	typedef struct
	{
		float FastHome;	/**< \english Speed used for first motion. \endenglish \russian Скорость первого движения. \endrussian */
		float SlowHome;	/**< \english Speed used for second motion. \endenglish \russian Скорость второго движения. \endrussian */
		float HomeDelta;	/**< \english Distance from break point. \endenglish \russian Расстояние отхода от точки останова. \endrussian */
		unsigned int HomeFlags;	/**< \english \ref flagset_homeflags "Home settings flags". \endenglish \russian \ref flagset_homeflags "Флаги настроек команды home". \endrussian */
	} home_settings_calb_t;

/** 
	* \english
	* Move settings.
	* \endenglish
	* \russian
	* Настройки движения.
	* \endrussian
	* @see set_move_settings
	* @see get_move_settings
	* @see get_move_settings, set_move_settings
	*/
	typedef struct
	{
		unsigned int Speed;	/**< \english Target speed (for stepper motor: steps/s, for DC: rpm). Range: 0..100000. \endenglish \russian Заданная скорость (для ШД: шагов/c, для DC: rpm). Диапазон: 0..100000. \endrussian */
		unsigned int uSpeed;	/**< \english Target speed in microstep fractions/s. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). Used with a stepper motor only. \endenglish \russian Заданная скорость в единицах деления микрошага в секунду. Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). Используется только с шаговым мотором. \endrussian */
		unsigned int Accel;	/**< \english Motor shaft acceleration, steps/s^2 (stepper motor) or RPM/s (DC). Range: 1..65535. \endenglish \russian Ускорение, заданное в шагах в секунду^2 (ШД) или в оборотах в минуту за секунду (DC). Диапазон: 1..65535. \endrussian */
		unsigned int Decel;	/**< \english Motor shaft deceleration, steps/s^2 (stepper motor) or RPM/s (DC). Range: 1..65535. \endenglish \russian Торможение, заданное в шагах в секунду^2 (ШД) или в оборотах в минуту за секунду (DC). Диапазон: 1..65535. \endrussian */
		unsigned int AntiplaySpeed;	/**< \english Speed in antiplay mode, full steps/s (stepper motor) or RPM (DC). Range: 0..100000. \endenglish \russian Скорость в режиме антилюфта, заданная в целых шагах/c (ШД) или в оборотах/с(DC). Диапазон: 0..100000. \endrussian */
		unsigned int uAntiplaySpeed;	/**< \english Speed in antiplay mode, microsteps/s. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). Used with a stepper motor only. \endenglish \russian Скорость в режиме антилюфта, выраженная в микрошагах в секунду. Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). Используется только с шаговым мотором. \endrussian */
		unsigned int MoveFlags;	/**< \english \ref flagset_moveflags "Flags of the motion parameters". \endenglish \russian \ref flagset_moveflags "Флаги параметров движения". \endrussian */
	} move_settings_t;

/** 
	* \english
	* User units move settings.
	* \endenglish
	* \russian
	* Настройки движения с использованием пользовательских единиц.
	* \endrussian
	* @see set_move_settings_calb
	* @see get_move_settings_calb
	* @see get_move_settings, set_move_settings
	*/
	typedef struct
	{
		float Speed;	/**< 
	 * \english 
	 * Speed 
	 * - For stepper motor without encoder — in steps per second.  
	 * - When using encoder (including DC/BLDC) — in revolutions per minute (RPM).  
	 * \endenglish 
	 * \russian 
	 * Скорость 
	 * - для шагового двигателя без энкодера — в шагах в секунду.  
	 * - при использовании энкодера (включая DC/BLDC) — в оборотах в минуту (RPM).  
	 * \endrussian 
	 */
		float Accel;	/**< 
	 * \english 
	 * Acceleration: 
	 * - For stepper motor without encoder — in steps per second^2.  
	 * - When using encoder (including DC/BLDC) — in revolutions per minute (RPM).  
	 * \endenglish 
	 * \russian 
	 * Ускорение  
	 * - для шагового двигателя без энкодера — в шагах в секунду^2.  
	 * - при использовании энкодера (включая DC/BLDC) — в оборотах в минуту (RPM).  
	 * \endrussian 
	 */
		float Decel;	/**< 
	 * \english 
	 * Deceleration  
	 * - For stepper motor without encoder — in steps per second^2.  
	 * - When using encoder (including DC/BLDC) — in revolutions per minute (RPM).  
	 * \endenglish 
	 * \russian 
	 * Торможение  
	 * - для шагового двигателя без энкодера — в шагах в секунду^2.  
	 * - при использовании энкодера (включая DC/BLDC) — в оборотах в минуту (RPM).  
	 * \endrussian 
	 */
		float AntiplaySpeed;	/**< 
	 * \english 
	 * Antiplay speed  
	 * - For stepper motor without encoder — in steps per second.  
	 * - When using encoder (including DC/BLDC) — in revolutions per minute (RPM).  
	 * \endenglish 
	 * \russian 
	 * Скорость в режиме антилюфта  
	 * - для шагового двигателя без энкодера — в шагах в секунду.  
	 * - при использовании энкодера (включая DC/BLDC) — в оборотах в минуту (RPM).  
	 * \endrussian 
	 */
		unsigned int MoveFlags;	/**< \english \ref flagset_moveflags "Flags of the motion parameters". \endenglish \russian \ref flagset_moveflags "Флаги параметров движения". \endrussian */
	} move_settings_calb_t;

/** 
	* \english
	* Movement limitations and settings related to the motor.
	* This structure contains useful motor settings. These settings specify the motor shaft movement algorithm, list of limitations and rated characteristics. All boards are supplied with the standard set of engine settings on the controller's flash memory. Please load new engine settings when you change the motor, encoder, positioner, etc. Please note that wrong engine settings may lead to device malfunction, which can lead to irreversible damage to the board.
	* \endenglish
	* \russian
	* Ограничения и настройки движения, связанные с двигателем.
	* Эта структура содержит настройки мотора.
	* Настройки определяют номинальные значения напряжения, тока, скорости мотора, характер движения и тип мотора.
	* Пожалуйста, загружайте новые настройки когда вы меняете мотор, энкодер или позиционер.
	* Помните, что неправильные настройки мотора могут повредить оборудование.
	* \endrussian
	* @see set_engine_settings
	* @see get_engine_settings
	* @see get_engine_settings, set_engine_settings
	*/
	typedef struct
	{
		unsigned int NomVoltage;	/**< \english Rated voltage in tens of mV. Controller will keep the voltage drop on motor below this value if ENGINE_LIMIT_VOLT flag is set (used with DC only). \endenglish \russian Номинальное напряжение мотора в десятках мВ. Контроллер будет сохранять напряжение на моторе не выше номинального, если установлен флаг ENGINE_LIMIT_VOLT (используется только с DC двигателем). \endrussian */
		unsigned int NomCurrent;	/**< \english Rated current (in mA). Controller will keep current consumed by motor below this value if ENGINE_LIMIT_CURR flag is set. Range: 15..8000 \endenglish \russian Номинальный ток через мотор (в мА). Ток стабилизируется для шаговых и может быть ограничен для DC(если установлен флаг ENGINE_LIMIT_CURR). Диапазон: 15..8000 \endrussian */
		unsigned int NomSpeed;	/**< \english Nominal (maximum) speed (in whole steps/s or rpm for DC and stepper motor as a master encoder). Controller will keep motor shaft RPM below this value if ENGINE_LIMIT_RPM flag is set. Range: 1..100000. \endenglish \russian Номинальная (максимальная) скорость (в целых шагах/с или rpm для DC и шагового двигателя в режиме ведущего энкодера). Контроллер будет сохранять скорость мотора не выше номинальной, если установлен флаг ENGINE_LIMIT_RPM. Диапазон: 1..100000. \endrussian */
		unsigned int uNomSpeed;	/**< \english The fractional part of a nominal speed in microsteps (is only used with stepper motor). Microstep size and the range of valid values for this field depend on selected step division mode (see MicrostepMode field in engine_settings). \endenglish \russian Микрошаговая часть номинальной скорости мотора (используется только с шаговым двигателем). Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
		unsigned int EngineFlags;	/**< \english \ref flagset_engineflags "Flags of engine settings". \endenglish \russian \ref flagset_engineflags "Флаги параметров мотора". \endrussian */
		int Antiplay;	/**< \english Number of pulses or steps for backlash (play) compensation procedure. Used if ENGINE_ANTIPLAY flag is set. \endenglish \russian Количество шагов двигателя или импульсов энкодера, на которое позиционер будет отъезжать от заданной позиции для подхода к ней с одной и той же стороны. Используется, если установлен флаг ENGINE_ANTIPLAY. \endrussian */
		unsigned int MicrostepMode;	/**< \english \ref flagset_microstepmode "Flags of microstep mode". \endenglish \russian \ref flagset_microstepmode "Флаги параметров микрошагового режима". \endrussian */
		unsigned int StepsPerRev;	/**< \english Number of full steps per revolution (Used with stepper motor only). Range: 1..65535. \endenglish \russian Количество полных шагов на оборот(используется только с шаговым двигателем). Диапазон: 1..65535. \endrussian */
	} engine_settings_t;

/** 
	* \english
	* Movement limitations and settings, related to the motor. In user units.
	*
	* This structure contains useful motor settings. These settings specify the motor shaft movement algorithm, list of limitations and rated characteristics. All boards are supplied with the standard set of engine settings on the controller's flash memory. Please load new engine settings when you change the motor, encoder, positioner, etc. Please note that wrong engine settings may lead to the device malfunction, that may cause irreversible damage to the board.
	* \endenglish
	* \russian
	* Ограничения и настройки движения, связанные с двигателем, с использованием пользовательских единиц.
	* Эта структура содержит настройки мотора.
	* Настройки определяют номинальные значения напряжения, тока, скорости мотора, характер движения и тип мотора.
	* Пожалуйста, загружайте новые настройки когда вы меняете мотор, энкодер или позиционер.
	* Помните, что неправильные настройки мотора могут повредить оборудование.
	* \endrussian
	* @see set_engine_settings_calb
	* @see get_engine_settings_calb
	* @see get_engine_settings, set_engine_settings
	*/
	typedef struct
	{
		unsigned int NomVoltage;	/**< \english Rated voltage in tens of mV. Controller will keep the voltage drop on motor below this value if ENGINE_LIMIT_VOLT flag is set (used with DC only). \endenglish \russian Номинальное напряжение мотора в десятках мВ. Контроллер будет сохранять напряжение на моторе не выше номинального, если установлен флаг ENGINE_LIMIT_VOLT (используется только с DC двигателем). \endrussian */
		unsigned int NomCurrent;	/**< \english Rated current (in mA). Controller will keep current consumed by motor below this value if ENGINE_LIMIT_CURR flag is set. Range: 15..8000 \endenglish \russian Номинальный ток через мотор (в мА). Ток стабилизируется для шаговых и может быть ограничен для DC(если установлен флаг ENGINE_LIMIT_CURR). Диапазон: 15..8000 \endrussian */
		float NomSpeed;	/**< \english Nominal speed. Controller will keep motor speed below this value if ENGINE_LIMIT_RPM flag is set. \endenglish \russian Номинальная скорость. Контроллер будет сохранять скорость мотора не выше номинальной, если установлен флаг ENGINE_LIMIT_RPM. \endrussian */
		unsigned int EngineFlags;	/**< \english \ref flagset_engineflags "Flags of engine settings". \endenglish \russian \ref flagset_engineflags "Флаги параметров мотора". \endrussian */
		float Antiplay;	/**< \english Number of pulses or steps for backlash (play) compensation procedure. Used if ENGINE_ANTIPLAY flag is set. \endenglish \russian Количество шагов двигателя или импульсов энкодера, на которое позиционер будет отъезжать от заданной позиции для подхода к ней с одной и той же стороны. Используется, если установлен флаг ENGINE_ANTIPLAY. \endrussian */
		unsigned int MicrostepMode;	/**< \english \ref flagset_microstepmode "Flags of microstep mode". \endenglish \russian \ref flagset_microstepmode "Флаги параметров микрошагового режима". \endrussian */
		unsigned int StepsPerRev;	/**< \english Number of full steps per revolution (Used with stepper motor only). Range: 1..65535. \endenglish \russian Количество полных шагов на оборот(используется только с шаговым двигателем). Диапазон: 1..65535. \endrussian */
	} engine_settings_calb_t;

/** 
	* \english
	* Engine type and driver type settings.
	* @param id An identifier of a device
	* @param EngineType engine type
	* @param DriverType driver type
	* \endenglish
	* \russian
	* Настройки типа мотора и типа силового драйвера.
	* Эта структура содержит настройки типа мотора и типа силового драйвера.
	* @param id идентификатор устройства
	* @param EngineType тип мотора
	* @param DriverType тип силового драйвера
	* \endrussian
	* @see get_entype_settings, set_entype_settings
	*/
	typedef struct
	{
		unsigned int EngineType;	/**< \english \ref flagset_enginetype "Flags of engine type". \endenglish \russian \ref flagset_enginetype "Флаги, определяющие тип мотора". \endrussian */
		unsigned int DriverType;	/**< \english \ref flagset_drivertype "Flags of driver type". \endenglish \russian \ref flagset_drivertype "Флаги, определяющие тип силового драйвера". \endrussian */
	} entype_settings_t;

/** 
	* \english
	* Step motor power settings.
	* \endenglish
	* \russian
	* Настройки питания шагового мотора.
	* \endrussian
	* @see set_move_settings
	* @see get_move_settings
	* @see get_power_settings, set_power_settings
	*/
	typedef struct
	{
		unsigned int HoldCurrent;	/**< \english Holding current, as percent of the nominal current. Range: 0..100. \endenglish \russian Ток мотора в режиме удержания, в процентах от номинального. Диапазон: 0..100. \endrussian */
		unsigned int CurrReductDelay;	/**< \english Time in ms from going to STOP state to the end of current reduction. \endenglish \russian Время в мс от перехода в состояние STOP до уменьшения тока. \endrussian */
		unsigned int PowerOffDelay;	/**< \english Time in s from going to STOP state to turning power off. \endenglish \russian Время в с от перехода в состояние STOP до отключения питания мотора. \endrussian */
		unsigned int CurrentSetTime;	/**< \english Time in ms to reach the nominal current. \endenglish \russian Время в мс, требуемое для набора номинального тока от 0% до 100%. \endrussian */
		unsigned int PowerFlags;	/**< \english \ref flagset_powerflags "Flags of power settings of stepper motor". \endenglish \russian \ref flagset_powerflags "Флаги параметров питания шагового мотора". \endrussian */
	} power_settings_t;

/** 
	* \english
	* This structure contains raw analog data from ADC embedded on board.
	* These data are used for device testing and deep recalibration by the manufacturer only.
	* \endenglish
	* \russian
	* Эта структура содержит необработанные данные с АЦП и нормированные значения.
	* Эти данные используются в сервисных целях для тестирования и калибровки устройства.
	* \endrussian
	* @see get_secure_settings
	* @see set_secure_settings
	* @see get_secure_settings, set_secure_settings
	*/
	typedef struct
	{
		unsigned int LowUpwrOff;	/**< \english Lower voltage limit to turn off the motor, in tens of mV. \endenglish \russian Нижний порог напряжения на силовой части для выключения, десятки мВ. \endrussian */
		unsigned int CriticalIpwr;	/**< \english Maximum motor current which triggers ALARM state, in mA. \endenglish \russian Максимальный ток силовой части, вызывающий состояние ALARM, в мА. \endrussian */
		unsigned int CriticalUpwr;	/**< \english Maximum motor voltage which triggers ALARM state, in tens of mV. \endenglish \russian Максимальное напряжение на силовой части, вызывающее состояние ALARM, десятки мВ. \endrussian */
		unsigned int CriticalT;	/**< \english Maximum temperature, which triggers ALARM state, in tenths of degrees Celsius. \endenglish \russian Максимальная температура контроллера, вызывающая состояние ALARM, в десятых долях градуса Цельсия.\endrussian */
		unsigned int CriticalIusb;	/**< \english Deprecated. Maximum USB current which triggers ALARM state, in mA. \endenglish \russian Устарело. Максимальный ток USB, вызывающий состояние ALARM, в мА. \endrussian */
		unsigned int CriticalUusb;	/**< \english Deprecated. Maximum USB voltage which triggers ALARM state, in tens of mV. \endenglish \russian Устарело. Максимальное напряжение на USB, вызывающее состояние ALARM, десятки мВ. \endrussian */
		unsigned int MinimumUusb;	/**< \english Deprecated. Minimum USB voltage which triggers ALARM state, in tens of mV. \endenglish \russian Устарело. Минимальное напряжение на USB, вызывающее состояние ALARM, десятки мВ. \endrussian */
		unsigned int Flags;	/**< \english \ref flagset_secureflags "Flags of secure settings". \endenglish \russian \ref flagset_secureflags "Флаги критических параметров". \endrussian */
	} secure_settings_t;

/**  
	* \english
	* Edges settings.
	* This structure contains border and limit switches settings. Please load new engine settings when you change positioner, etc. Please note that wrong engine settings may lead to device malfunction, which can cause irreversible damage to the board.
	* \endenglish
	* \russian
	* Настройки границ.
	* Эта структура содержит настройки границ и концевых выключателей.
	* Пожалуйста, загружайте новые настройки когда вы меняете позиционер.
	* Помните, что неправильные настройки мотора могут повредить оборудование.
	* \endrussian
	* @see set_edges_settings
	* @see get_edges_settings
	* @see get_edges_settings, set_edges_settings
	*/
	typedef struct
	{
		unsigned int BorderFlags;	/**< \english \ref flagset_borderflags "Border flags". \endenglish \russian \ref flagset_borderflags "Флаги границ". \endrussian */
		unsigned int EnderFlags;	/**< \english \ref flagset_enderflags "Limit switches flags". \endenglish \russian \ref flagset_enderflags "Флаги концевых выключателей". \endrussian */
		int LeftBorder;	/**< \english Left border position, used if BORDER_IS_ENCODER flag is set. \endenglish \russian Позиция левой границы, используется если установлен флаг BORDER_IS_ENCODER. \endrussian */
		int uLeftBorder;	/**< \english Left border position in microsteps (used with stepper motor only). The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). \endenglish \russian Позиция левой границы в микрошагах (используется только с шаговым двигателем). Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
		int RightBorder;	/**< \english Right border position, used if BORDER_IS_ENCODER flag is set. \endenglish \russian Позиция правой границы, используется если установлен флаг BORDER_IS_ENCODER. \endrussian */
		int uRightBorder;	/**< \english Right border position in microsteps. Used with a stepper motor only. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). \endenglish \russian Позиция правой границы в микрошагах (используется только с шаговым двигателем). Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
	} edges_settings_t;

/**  
	* \english
	* User unit edges settings.
	* This structure contains border and limit switches settings. Please load new engine settings when you change positioner, etc. Please note that wrong engine settings may lead to device malfunction, which can cause irreversible damage to the board.
	* \endenglish
	* \russian
	* Настройки границ с использованием пользовательских единиц.
	* Эта структура содержит настройки границ и концевых выключателей.
	* Пожалуйста, загружайте новые настройки когда вы меняете позиционер.
	* Помните, что неправильные настройки мотора могут повредить оборудование.
	* \endrussian
	* @see set_edges_settings_calb
	* @see get_edges_settings_calb
	* @see get_edges_settings, set_edges_settings
	*/
	typedef struct
	{
		unsigned int BorderFlags;	/**< \english \ref flagset_borderflags "Border flags". \endenglish \russian \ref flagset_borderflags "Флаги границ". \endrussian */
		unsigned int EnderFlags;	/**< \english \ref flagset_enderflags "Limit switches flags". \endenglish \russian \ref flagset_enderflags "Флаги концевых выключателей". \endrussian */
		float LeftBorder;	/**< \english Left border position, used if BORDER_IS_ENCODER flag is set. Corrected by the table. \endenglish \russian Позиция левой границы, используется если установлен флаг BORDER_IS_ENCODER. Корректируется таблицей. \endrussian */
		float RightBorder;	/**< \english Right border position, used if BORDER_IS_ENCODER flag is set. Corrected by the table. \endenglish \russian Позиция правой границы, используется если установлен флаг BORDER_IS_ENCODER. Корректируется таблицей. \endrussian */
	} edges_settings_calb_t;

/**  
	* \english
	* PID settings.
	* This structure contains factors for PID routine. It specifies the behavior of the voltage PID routine. These factors are slightly different for different positioners. All boards are supplied with the standard set of PID settings in the controller's flash memory. Please load new PID settings when you change positioner. Please note that wrong PID settings lead to device malfunction.
	* \endenglish
	* \russian
	* Настройки ПИД.
	* Эта структура содержит коэффициенты для ПИД регулятора.
	* Они определяют работу ПИД контура напряжения.
	* Эти коэффициенты хранятся во flash памяти контроллера.
	* Пожалуйста, загружайте новые настройки, когда вы меняете мотор или позиционер.
	* Помните, что неправильные настройки ПИД контуров могут повредить оборудование.
	* \endrussian
	* @see set_pid_settings
	* @see get_pid_settings
	* @see get_pid_settings, set_pid_settings
	*/
	typedef struct
	{
		unsigned int KpU;	/**< \english Proportional gain for voltage PID routine \endenglish \russian Пропорциональный коэффициент ПИД контура по напряжению \endrussian */
		unsigned int KiU;	/**< \english Integral gain for voltage PID routine \endenglish \russian Интегральный коэффициент ПИД контура по напряжению \endrussian */
		unsigned int KdU;	/**< \english Differential gain for voltage PID routine \endenglish \russian Дифференциальный коэффициент ПИД контура по напряжению \endrussian */
		float Kpf;	/**< \english Proportional gain for BLDC position PID routine \endenglish \russian Пропорциональный коэффициент ПИД контура по позиции для BLDC \endrussian */
		float Kif;	/**< \english Integral gain for BLDC position PID routine \endenglish \russian Интегральный коэффициент ПИД контура по позиции для BLDC \endrussian */
		float Kdf;	/**< \english Differential gain for BLDC position PID routine \endenglish \russian Дифференциальный коэффициент ПИД контура по позиции для BLDC \endrussian */
	} pid_settings_t;

/** 
	* \english
	* Synchronization settings.
	*
	* This structure contains all synchronization settings, modes, periods and flags. It specifies the behavior of the input synchronization. All boards are supplied with the standard set of these settings.
	* \endenglish
	* \russian
	* Настройки входной синхронизации.
	* Эта структура содержит все настройки, определяющие поведение входа синхронизации.
	* \endrussian
	* @see get_sync_in_settings
	* @see set_sync_in_settings
	* @see get_sync_in_settings, set_sync_in_settings
	*/
	typedef struct
	{
		unsigned int SyncInFlags;	/**< \english \ref flagset_syncinflags "Flags for synchronization input setup". \endenglish \russian \ref flagset_syncinflags "Флаги настроек синхронизации входа". \endrussian */
		unsigned int ClutterTime;	/**< \english Input synchronization pulse dead time (us). \endenglish \russian Минимальная длительность входного импульса синхронизации для защиты от дребезга (мкс). \endrussian */
		int Position;	/**< \english Desired position or shift (full steps) \endenglish \russian Желаемая позиция или смещение (в полных шагах) \endrussian */
		int uPosition;	/**< \english The fractional part of a position or shift in microsteps. It is used with a stepper motor. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). \endenglish \russian Дробная часть позиции или смещения в микрошагах. Используется только с шаговым двигателем. Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
		unsigned int Speed;	/**< \english Target speed (for stepper motor: steps/s, for DC: rpm). Range: 0..100000. \endenglish \russian Заданная скорость (для ШД: шагов/c, для DC: rpm). Диапазон: 0..100000. \endrussian */
		unsigned int uSpeed;	/**< \english Target speed in microsteps/s. Microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). Used a stepper motor only. \endenglish \russian Заданная скорость в микрошагах в секунду. Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). Используется только с шаговым мотором. \endrussian */
	} sync_in_settings_t;

/** 
	* \english
	* User unit synchronization settings.
	* This structure contains all synchronization settings, modes, periods and flags. It specifies behavior of input synchronization. All boards are supplied with the standard set of these settings.
	* \endenglish
	* \russian
	* Настройки входной синхронизации с использованием пользовательских единиц.
	* Эта структура содержит все настройки, определяющие поведение входа синхронизации.
	* \endrussian
	* @see get_sync_in_settings_calb
	* @see set_sync_in_settings_calb
	* @see get_sync_in_settings, set_sync_in_settings
	*/
	typedef struct
	{
		unsigned int SyncInFlags;	/**< \english \ref flagset_syncinflags "Flags for synchronization input setup". \endenglish \russian \ref flagset_syncinflags "Флаги настроек синхронизации входа". \endrussian */
		unsigned int ClutterTime;	/**< \english Input synchronization pulse dead time (us). \endenglish \russian Минимальная длительность входного импульса синхронизации для защиты от дребезга (мкс). \endrussian */
		float Position;	/**< \english Desired position or shift. \endenglish \russian Желаемая позиция или смещение. \endrussian */
		float Speed;	/**< \english Target speed. \endenglish \russian Заданная скорость. \endrussian */
	} sync_in_settings_calb_t;

/** 
	* \english
	* Synchronization settings.
	* This structure contains all synchronization settings, modes, periods and flags. It specifies the behavior of the output synchronization. All boards are supplied with the standard set of these settings.
	* \endenglish
	* \russian
	* Настройки выходной синхронизации.
	* Эта структура содержит все настройки, определяющие поведение выхода синхронизации.
	* \endrussian
	* @see get_sync_out_settings
	* @see set_sync_out_settings
	* @see get_sync_out_settings, set_sync_out_settings
	*/
	typedef struct
	{
		unsigned int SyncOutFlags;	/**< \english \ref flagset_syncoutflags "Flags of synchronization output". \endenglish \russian \ref flagset_syncoutflags "Флаги настроек синхронизации выхода". \endrussian */
		unsigned int SyncOutPulseSteps;	/**< \english This value specifies the duration of output pulse. It is measured microseconds when SYNCOUT_IN_STEPS flag is cleared or in encoder pulses or motor steps when SYNCOUT_IN_STEPS is set. \endenglish \russian Определяет длительность выходных импульсов в шагах/импульсах энкодера, когда установлен флаг SYNCOUT_IN_STEPS, или в микросекундах если флаг сброшен. \endrussian */
		unsigned int SyncOutPeriod;	/**< \english This value specifies the number of encoder pulses or steps between two output synchronization pulses when SYNCOUT_ONPERIOD is set. \endenglish \russian Период генерации импульсов (в шагах/отсчетах энкодера), используется при установленном флаге SYNCOUT_ONPERIOD. \endrussian */
		unsigned int Accuracy;	/**< \english This is the neighborhood around the target coordinates, every point in which is treated as the target position. Getting in these points cause the stop impulse. \endenglish \russian Это окрестность вокруг целевой координаты, попадание в которую считается попаданием в целевую позицию и генерируется импульс по остановке. \endrussian */
		unsigned int uAccuracy;	/**< \english This is the neighborhood around the target coordinates in microsteps (used with a stepper motor only). The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). \endenglish \russian Это окрестность вокруг целевой координаты в микрошагах (используется только с шаговым двигателем). Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
	} sync_out_settings_t;

/** 
	* \english
	* Synchronization settings which use user units.
	*
	* This structure contains all synchronization settings, modes, periods and flags. It specifies the behavior of the output synchronization. All boards are supplied with the standard set of these settings.
	* \endenglish
	* \russian
	* Настройки выходной синхронизации с использованием пользовательских единиц.
	* Эта структура содержит все настройки, определяющие поведение выхода синхронизации.
	* \endrussian
	* @see get_sync_out_settings_calb
	* @see set_sync_out_settings_calb
	* @see get_sync_out_settings, set_sync_out_settings
	*/
	typedef struct
	{
		unsigned int SyncOutFlags;	/**< \english \ref flagset_syncoutflags "Flags of synchronization output". \endenglish \russian \ref flagset_syncoutflags "Флаги настроек синхронизации выхода". \endrussian */
		unsigned int SyncOutPulseSteps;	/**< \english This value specifies the duration of output pulse. It is measured microseconds when SYNCOUT_IN_STEPS flag is cleared or in encoder pulses or motor steps when SYNCOUT_IN_STEPS is set. \endenglish \russian Определяет длительность выходных импульсов в шагах/импульсах энкодера, когда установлен флаг SYNCOUT_IN_STEPS, или в микросекундах если флаг сброшен. \endrussian */
		unsigned int SyncOutPeriod;	/**< \english This value specifies the number of encoder pulses or steps between two output synchronization pulses when SYNCOUT_ONPERIOD is set. \endenglish \russian Период генерации импульсов (в шагах/отсчетах энкодера), используется при установленном флаге SYNCOUT_ONPERIOD. \endrussian */
		float Accuracy;	/**< \english This is the neighborhood around the target coordinates, every point in which is treated as the target position. Getting in these points cause the stop impulse. \endenglish \russian Это окрестность вокруг целевой координаты (в шагах/отсчетах энкодера), попадание в которую считается попаданием в целевую позицию и генерируется импульс по остановке. \endrussian */
	} sync_out_settings_calb_t;

/** 
	* \english
	* EXTIO settings.
	*
	* This structure contains all EXTIO settings. By default, input events are signaled through a rising front, and output states are signaled by a high logic state.
	* \endenglish
	* \russian
	* Настройки EXTIO.
	* Эта структура содержит все настройки, определяющие поведение ножки EXTIO.
	* Входные события обрабатываются по фронту. Выходные состояния сигнализируются логическим состоянием.
	* По умолчанию нарастающий фронт считается моментом подачи входного сигнала, а единичное состояние считается активным выходом.
	* \endrussian
	* @see get_extio_settings
	* @see set_extio_settings
	* @see get_extio_settings, set_extio_settings
	*/
	typedef struct
	{
		unsigned int EXTIOSetupFlags;	/**< \english \ref flagset_extiosetupflags "External IO setup flags". \endenglish \russian \ref flagset_extiosetupflags "Флаги настройки работы внешнего ввода/вывода". \endrussian */
		unsigned int EXTIOModeFlags;	/**< \english \ref flagset_extiomodeflags "External IO mode flags". \endenglish \russian \ref flagset_extiomodeflags "Флаги настройки режимов внешнего ввода/вывода". \endrussian */
	} extio_settings_t;

/** 
	* \english
	* Brake settings.
	*
	* This structure contains brake control parameters.
	* \endenglish
	* \russian
	* Настройки тормоза.
	* Эта структура содержит параметры управления тормозом.
	* \endrussian
	* @see set_brake_settings
	* @see get_brake_settings
	* @see get_brake_settings, set_brake_settings
	*/
	typedef struct
	{
		unsigned int t1;	/**< \english Time in ms between turning on motor power and turning off the brake. \endenglish \russian Время в мс между включением питания мотора и отключением тормоза. \endrussian */
		unsigned int t2;	/**< \english Time in ms between the brake turning off and moving readiness. All moving commands will execute after this interval. \endenglish \russian Время в мс между отключением тормоза и готовностью к движению. Все команды движения начинают выполняться только по истечении этого времени. \endrussian */
		unsigned int t3;	/**< \english Time in ms between motor stop and the brake turning on. \endenglish \russian Время в мс между остановкой мотора и включением тормоза. \endrussian */
		unsigned int t4;	/**< \english Time in ms between turning on the brake and turning off motor power. \endenglish \russian Время в мс между включением тормоза и отключением питания мотора. \endrussian */
		unsigned int BrakeFlags;	/**< \english \ref flagset_brakeflags "Brake settings flags". \endenglish \russian \ref flagset_brakeflags "Флаги настроек тормоза". \endrussian */
	} brake_settings_t;

/** 
	* \english
	* Control settings.
	*
	* This structure contains control parameters.
	*
    * In case of CTL_MODE=1, the joystick motor control is enabled. In this mode, while the joystick is maximally displaced, the engine tends to move at MaxSpeed[i]. i=0 if another value hasn't been set at the previous usage. To change the speed index "i", use the buttons.
	*
    * In case of CTL_MODE=2, the motor is controlled by the left/right buttons. When you click on the button, the motor starts moving in the appropriate direction at a speed MaxSpeed[0]. After Timeout[i], motor moves at speed MaxSpeed[i+1]. At the transition between MaxSpeed[i] and MaxSpeed[i+1] the motor just accelerates/decelerates as usual.
	* \endenglish
	* \russian
	* Настройки управления.
	* При выборе CTL_MODE=1 включается управление мотором с помощью джойстика.
	* В этом режиме при отклонении джойстика на максимум двигатель стремится
	* двигаться со скоростью MaxSpeed [i], где i=0, если предыдущим использованием
	* этого режима не было выбрано другое i. Кнопки переключают номер скорости i.
	* При выборе CTL_MODE=2 включается управление мотором с помощью кнопок
	* left/right. При нажатии на кнопки двигатель начинает двигаться в соответствующую сторону со скоростью MaxSpeed [0], по истечении времени Timeout[i] мотор
	* двигается со скоростью MaxSpeed [i+1]. При
	* переходе от MaxSpeed [i] на MaxSpeed [i+1] действует ускорение, как обычно.
	* \endrussian
	* @see set_control_settings
	* @see get_control_settings
	* @see get_control_settings, set_control_settings
	*/
	typedef struct
	{
		unsigned int MaxSpeed[10];	/**< \english Array of speeds (full step) used with the joystick and the button control. Range: 0..100000. \endenglish \russian Массив скоростей (в полных шагах), использующийся при управлении джойстиком или кнопками влево/вправо. Диапазон: 0..100000. \endrussian */
		unsigned int uMaxSpeed[10];	/**< \english Array of speeds (in microsteps) used with the joystick and the button control. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). \endenglish \russian Массив скоростей (в микрошагах), использующийся при управлении джойстиком или кнопками влево/вправо. Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
		unsigned int Timeout[9];	/**< \english Timeout[i] is timeout in ms. After that, max_speed[i+1] is applied. It's used with the button control only. \endenglish \russian timeout[i] - время в мс, по истечении которого устанавливается скорость max_speed[i+1] (используется только при управлении кнопками). \endrussian */
		unsigned int MaxClickTime;	/**< \english Maximum click time (in ms). Until the expiration of this time, the first speed isn't applied. \endenglish \russian Максимальное время клика (в мс). До истечения этого времени первая скорость не включается. \endrussian */
		unsigned int Flags;	/**< \english \ref flagset_controlflags "Control flags". \endenglish \russian \ref flagset_controlflags "Флаги управления". \endrussian */
		int DeltaPosition;	/**< \english Position Shift (delta) (full step) \endenglish \russian Смещение (дельта) позиции (в полных шагах) \endrussian */
		int uDeltaPosition;	/**< \english Fractional part of the shift in micro steps. It's used with a stepper motor only. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). \endenglish \russian Дробная часть смещения в микрошагах. Используется только с шаговым двигателем. Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
	} control_settings_t;

/** 
	* \english
	* User unit control settings.
	*
	* This structure contains control parameters.
	*
    * In case of CTL_MODE=1, the joystick motor control is enabled. In this mode, while the joystick is maximally displaced, the engine tends to move at MaxSpeed[i]. i=0 if another value hasn't been set at the previous usage. To change the speed index "i", use the buttons.
	*
    * In case of CTL_MODE=2, the motor is controlled by the left/right buttons. When you click on the button, the motor starts moving in the appropriate direction at a speed MaxSpeed[0]. After Timeout[i], the motor moves at speed MaxSpeed[i+1]. At the transition between MaxSpeed[i] and MaxSpeed[i+1] the motor just accelerates/decelerates as usual.
	* \endenglish
	* \russian
	* Настройки управления с использованием пользовательских единиц.
	* При выборе CTL_MODE=1 включается управление мотором с помощью джойстика.
	* В этом режиме при отклонении джойстика на максимум двигатель стремится
	* двигаться со скоростью MaxSpeed [i], где i=0, если предыдущим использованием
	* этого режима не было выбрано другое i. Кнопки переключают номер скорости i.
	* При выборе CTL_MODE=2 включается управление мотором с помощью кнопок
	* left/right. При нажатии на кнопки двигатель начинает двигаться в соответствующую сторону со скоростью MaxSpeed [0], по истечении времени Timeout[i] мотор
	* двигается со скоростью MaxSpeed [i+1]. При
	* переходе от MaxSpeed [i] на MaxSpeed [i+1] действует ускорение, как обычно.
	* \endrussian
	* @see set_control_settings_calb
	* @see get_control_settings_calb
	* @see get_control_settings, set_control_settings
	*/
	typedef struct
	{
		float MaxSpeed[10];	/**< \english Array of speeds used with the joystick and the button control. \endenglish \russian Массив скоростей, использующийся при управлении джойстиком или кнопками влево/вправо. \endrussian */
		unsigned int Timeout[9];	/**< \english Timeout[i] is timeout in ms. After that, max_speed[i+1] is applied. It's used with the button control only. \endenglish \russian timeout[i] - время в мс, по истечении которого устанавливается скорость max_speed[i+1] (используется только при управлении кнопками). \endrussian */
		unsigned int MaxClickTime;	/**< \english Maximum click time (in ms). Until the expiration of this time, the first speed isn't applied. \endenglish \russian Максимальное время клика (в мс). До истечения этого времени первая скорость не включается. \endrussian */
		unsigned int Flags;	/**< \english \ref flagset_controlflags "Control flags". \endenglish \russian \ref flagset_controlflags "Флаги управления". \endrussian */
		float DeltaPosition;	/**< \english Position shift (delta) \endenglish \russian Смещение (дельта) позиции \endrussian */
	} control_settings_calb_t;

/** 
	* \english
	* Joystick settings.
	*
	* This structure contains joystick parameters.
        * If joystick position falls outside DeadZone limits, a movement begins. The speed is defined by the joystick's position
	* in the range from the DeadZone limit to the maximum deviation. Joystick positions inside DeadZone limits
        * correspond to zero speed (a "soft stop" command is issued continuously),
	* and positions beyond Low and High limits correspond to MaxSpeed[i] or -MaxSpeed[i] (see command SCTL),
	* where i = 0 by default and can be changed with the left/right buttons (see command SCTL).
	* If the next speed in the list is zero (both integer and microstep parts), the button press is ignored.
        * The first speed in the list shouldn't be zero. DeadZone is defined in 0.1% units.
	*
	* The relationship between the deviation and the rate is exponential, which allows for high mobility and accuracy without speed mode switching.
	* \endenglish
	* \russian
	* Настройки джойстика.
	* Команда чтения настроек и калибровки джойстика.
	* При отклонении джойстика более чем на DeadZone от центрального положения начинается движение со скоростью,
	* определяемой отклонением джойстика от DeadZone до 100% отклонения, причем отклонению DeadZone соответствует
	* нулевая скорость (при этом постоянно выполняется команда "soft stop"), а 100% отклонения соответствует MaxSpeed [i] (см. команду SCTL),
        * где i=0, если предыдущим использованием этого режима не было выбрано другое i.
	* Если следующая скорость в таблице скоростей нулевая (целая и микрошаговая части), то перехода на неё не происходит.
	* Первая скорость в списке не должна быть нулевой. DeadZone вычисляется в десятых долях процента отклонения
	* от центра (JoyCenter) до правого или левого максимума.
        * Зависимость между отклонением и скоростью экспоненциальная,
	* что позволяет без переключения режимов скорости сочетать высокую подвижность и точность.
	* \endrussian
	* @see set_joystick_settings
	* @see get_joystick_settings
	* @see get_joystick_settings, set_joystick_settings
	*/
	typedef struct
	{
		unsigned int JoyLowEnd;	/**< \english Joystick lower end position. Range: 0..10000. \endenglish \russian Значение в шагах джойстика, соответствующее нижней границе диапазона отклонения устройства. Должно лежать в пределах. Диапазон: 0..10000. \endrussian */
		unsigned int JoyCenter;	/**< \english Joystick center position. Range: 0..10000. \endenglish \russian Значение в шагах джойстика, соответствующее неотклонённому устройству. Должно лежать в пределах. Диапазон: 0..10000. \endrussian */
		unsigned int JoyHighEnd;	/**< \english Joystick upper end position. Range: 0..10000. \endenglish \russian Значение в шагах джойстика, соответствующее верхней границе диапазона отклонения устройства. Должно лежать в пределах. Диапазон: 0..10000. \endrussian */
		unsigned int ExpFactor;	/**< \english Exponential nonlinearity factor. \endenglish \russian Фактор экспоненциальной нелинейности отклика джойстика. \endrussian */
		unsigned int DeadZone;	/**< \english Joystick deviation from the central position in 0.1% units that do not result in a start of motion. Maximal deviation is +-25.5% that is more than a half of the total joystick range. \endenglish \russian Отклонение от среднего положения, которое не вызывает начала движения (в десятых долях процента). Максимальное мёртвое отклонение +-25.5%, что составляет половину рабочего диапазона джойстика. \endrussian */
		unsigned int JoyFlags;	/**< \english \ref flagset_joyflags "Joystick flags". \endenglish \russian \ref flagset_joyflags "Флаги джойстика". \endrussian */
	} joystick_settings_t;

/** 
	* \english
	* Control position settings (used with stepper motor only)
	*
	* When controlling the step motor with the encoder (CTP_BASE=0), it is possible to detect the loss of steps. The controller knows the number of steps per revolution (GENG::StepsPerRev) and the encoder resolution (GFBS::IPT). When the control is enabled (CTP_ENABLED is set), the controller stores the current position in the steps of SM and the current position of the encoder. Next, the encoder position is converted into steps at each step, and if the difference between the current position in steps and the encoder position is greater than CTPMinError, the flag STATE_CTP_ERROR is set.
    *
	* Alternatively, the stepper motor may be controlled with the speed sensor (CTP_BASE 1). In this mode, at the active edges of the input clock, the controller stores the current value of steps. Then, at each revolution, the controller checks how many steps have been passed. When the difference is over the CTPMinError, the STATE_CTP_ERROR flag is set.
	* \endenglish
	* \russian
	* Настройки контроля позиции(для шагового двигателя).
	* При управлении ШД с энкодером (CTP_BASE 0) появляется возможность обнаруживать потерю шагов. Контроллер знает кол-во шагов на оборот (GENG::StepsPerRev) и разрешение энкодера (GFBS::IPT). При включении контроля (флаг CTP_ENABLED), контроллер запоминает текущую позицию в шагах ШД и текущую позицию энкодера. Далее, на каждом шаге позиция энкодера преобразовывается в шаги и если разница оказывается больше CTPMinError, устанавливается флаг STATE_CTP_ERROR и устанавливается состояние ALARM.
    *
	* При управлении ШД с датчиком оборотов (CTP_BASE 1), позиция контролируется по нему. По активному фронту на входе синхронизации контроллер запоминает текущее значение шагов. Далее, при каждом обороте проверяет, на сколько шагов сместились. При рассогласовании более CTPMinError устанавливается флаг STATE_CTP_ERROR и устанавливается состояние ALARM.
	* \endrussian
	* @see set_ctp_settings
	* @see get_ctp_settings
	* @see get_ctp_settings, set_ctp_settings
	*/
	typedef struct
	{
		unsigned int CTPMinError;	/**< \english The minimum difference between the SM position in steps and the encoder position that causes the setting of the STATE_CTP_ERROR flag. Measured in steps. \endenglish \russian Минимальное отличие шагов ШД от положения энкодера, устанавливающее флаг STATE_RT_ERROR. Измеряется в шагах ШД. \endrussian */
		unsigned int CTPFlags;	/**< \english \ref flagset_ctpflags "Position control flags". \endenglish \russian \ref flagset_ctpflags "Флаги контроля позиции". \endrussian */
	} ctp_settings_t;

/** 
	* \english
	* UART settings.
	*
	* This structure contains UART settings.
	* \endenglish
	* \russian
	* Настройки UART.
	* Эта структура содержит настройки UART.
	* \endrussian
	* @see get_uart_settings
	* @see set_uart_settings
	* @see get_uart_settings, set_uart_settings
	*/
	typedef struct
	{
		unsigned int Speed;	/**< \english UART baudrate (in bauds) \endenglish \russian Cкорость UART (в бодах) \endrussian */
		unsigned int UARTSetupFlags;	/**< \english \ref flagset_uartsetupflags "UART parity flags". \endenglish \russian \ref flagset_uartsetupflags "Флаги настроек четности команды UART". \endrussian */
	} uart_settings_t;

/** 
	* \english
	* Network settings. Manufacturer only.
	* This structure contains network settings.
	* \endenglish
	* \russian
	* Настройки сети. Только для производителя.
	* Эта структура содержит настройки сети.
	* \endrussian
	* @see get_network_settings
	* @see set_network_settings
	* @see get_network_settings, set_network_settings
	*/
	typedef struct
	{
		unsigned int DHCPEnabled;	/**< \english Indicates the method to get the IP-address. It can be either 0 (static) or 1 (DHCP). \endenglish \russian Определяет способ получения IP-адреса каналов. Может принимать значения: 0 — статически, 1 — через DHCP \endrussian */
		unsigned int IPv4Address[4];	/**< \english IP-address of the device in format x.x.x.x. \endenglish \russian IP-адрес устройства в формате x.x.x.x. \endrussian */
		unsigned int SubnetMask[4];	/**< \english The mask of the subnet in format x.x.x.x. \endenglish \russian Маска подсети в формате x.x.x.x. \endrussian */
		unsigned int DefaultGateway[4];	/**< \english Default value of the gateway in format x.x.x.x. \endenglish \russian Шлюз сети по умолчанию в формате x.x.x.x. \endrussian */
	} network_settings_t;

/** 
	* \english
	* The web-page user password. Manufacturer only.
	* This structure contains the user password.
	* \endenglish
	* \russian
	* Пароль. Только для производителя.
	* Эта структура содержит пароль к веб-странице.
	* \endrussian
	* @see get_password_settings
	* @see set_password_settings
	* @see get_password_settings, set_password_settings
	*/
	typedef struct
	{
		char UserPassword[21];	/**< \english Password for the web-page that the user can change with a USB command or via web-page. \endenglish \russian Строчка-пароль для доступа к веб-странице, который пользователь может поменять с помощью USB команды или на веб-странице. \endrussian */
	} password_settings_t;

/** 
	* \english
	* Calibration settings.
	*
	* This structure contains calibration settings. These settings are used to convert bare ADC values to winding currents in mA and the full current in mA. Parameters are grouped into pairs, XXX_A and XXX_B, representing linear equation coefficients. The first one is the slope, the second one is the constant term. Thus, XXX_Current[mA] = XXX_A[mA/ADC]*XXX_ADC_CODE[ADC] + XXX_B[mA].
	* \endenglish
	* \russian
	* Калибровочные коэффициенты.
	* Эта структура содержит калибровочные коэффициенты. Эти коэффициенты используются для пересчёта кодов АЦП в токи обмоток и полный ток потребления. Коэффициенты сгруппированы в пары, XXX_A и XXX_B; пары представляют собой коэффициенты линейного уравнения. Первый коэффициент - тангенс угла наклона, второй - постоянное смещение. Таким образом, XXX_Current[mA] = XXX_A[mA/ADC]*XXX_ADC_CODE[ADC] + XXX_B[mA].
	* \endrussian
	* @see get_calibration_settings
	* @see set_calibration_settings
	* @see get_calibration_settings, set_calibration_settings
	*/
	typedef struct
	{
		float CSS1_A;	/**< \english Scaling factor for the analog measurements of the A winding current. \endenglish \russian Коэффициент масштабирования для аналоговых измерений тока в обмотке A. \endrussian */
		float CSS1_B;	/**< \english Offset for the analog measurements of the A winding current. \endenglish \russian Коэффициент сдвига для аналоговых измерений тока в обмотке A. \endrussian */
		float CSS2_A;	/**< \english Scaling factor for the analog measurements of the B winding current. \endenglish \russian Коэффициент масштабирования для аналоговых измерений тока в обмотке B. \endrussian */
		float CSS2_B;	/**< \english Offset for the analog measurements of the B winding current. \endenglish \russian Коэффициент сдвига для аналоговых измерений тока в обмотке B. \endrussian */
		float FullCurrent_A;	/**< \english Scaling factor for the analog measurements of the full current. \endenglish \russian Коэффициент масштабирования для аналоговых измерений полного тока. \endrussian */
		float FullCurrent_B;	/**< \english Offset for the analog measurements of the full current. \endenglish \russian Коэффициент сдвига для аналоговых измерений полного тока. \endrussian */
	} calibration_settings_t;

/** 
	* \english
	* Controller name and settings flags
	* \endenglish
	* \russian
	* Пользовательское имя контроллера и флаги настройки.
	* \endrussian
	* @see get_controller_name, set_controller_name
	*/
	typedef struct
	{
		char ControllerName[17];	/**< \english User controller name. It may be set by the user. Max string length: 16 characters. \endenglish \russian Пользовательское имя контроллера. Может быть установлено пользователем для его удобства. Максимальная длина строки: 16 символов. \endrussian */
		unsigned int CtrlFlags;	/**< \english \ref flagset_controllerflags "Flags of internal controller settings". \endenglish \russian \ref flagset_controllerflags "Флаги настроек контроллера". \endrussian */
	} controller_name_t;

/** 
	* \english
	* Structure contains user data to save into the FRAM.
	* \endenglish
	* \russian
	* Пользовательские данные для сохранения во FRAM.
	* \endrussian
	* @see get_nonvolatile_memory, set_nonvolatile_memory
	*/
	typedef struct
	{
		unsigned int UserData[7];	/**< \english User data. It may be set by the user. Each element of the array stores only 32 bits of user data. This is important on systems where an int type contains more than 4 bytes. For example, on all amd64 systems. \endenglish \russian Пользовательские данные. Могут быть установлены пользователем для его удобства. Каждый элемент массива хранит только 32 бита пользовательских данных. Это важно на системах где тип int содержит больше чем 4 байта. Например это все системы amd64.\endrussian */
	} nonvolatile_memory_t;

/**  
	* \english
	* EMF settings.
	*
	* This structure contains the data for Electromechanical characteristics (EMF) of the motor. It determines the inductance, resistance, and Electromechanical coefficient of the motor. This data is stored in the flash memory of the controller. Please set new settings when you change the motor. Remember that improper EMF settings may damage the equipment.
	* \endenglish
	* \russian
	* Настройки EMF.
	* Эта структура содержит данные электромеханических характеристик(EMF) двигателя.
	* Они определяют индуктивность, сопротивление и электромеханический коэффициент двигателя.
	* Эти данные хранятся во flash памяти контроллера.
	* Пожалуйста, загружайте новые настройки, когда вы меняете мотор.
	* Помните, что неправильные настройки EMF могут повредить оборудование.
	* \endrussian
	* @see set_emf_settings
	* @see get_emf_settings
	* @see get_emf_settings, set_emf_settings
	*/
	typedef struct
	{
		float L;	/**< \english Motor winding inductance. \endenglish \russian Индуктивность обмоток двигателя. \endrussian */
		float R;	/**< \english Motor winding resistance. \endenglish \russian Сопротивление обмоток двигателя. \endrussian */
		float Km;	/**< \english Electromechanical ratio of the motor. \endenglish \russian Электромеханический коэффициент двигателя. \endrussian */
		unsigned int BackEMFFlags;	/**< \english \ref flagset_backemfflags "Flags of auto-detection of characteristics of windings of the engine". \endenglish \russian \ref flagset_backemfflags "Флаги автоопределения характеристик обмоток двигателя". \endrussian */
	} emf_settings_t;

/**  
	* \english
	* EAS settings.
	*
	* This structure is intended for setting parameters of algorithms that cannot be attributed to standard Kp, Ki, Kd, and L, R, Km.
	* \endenglish
	* \russian
	* Настройки EAS.
	* Эта структура предназначена для настройки параметров алгоритмов, которые невозможно отнести к стандартным Kp, Ki, Kd и L, R, Km.
	* Эти данные хранятся во flash памяти контроллера.
	* \endrussian
	* @see set_engine_advansed_setup
	* @see get_engine_advansed_setup
	* @see get_engine_advansed_setup, set_engine_advansed_setup
	*/
	typedef struct
	{
		unsigned int stepcloseloop_Kw;	/**< \english Mixing ratio of the actual and set speed, range [0, 100], default value 50. \endenglish \russian Коэффициент смешения реальной и заданной скорости, диапазон [0, 100], значение по умолчанию 50. \endrussian */
		unsigned int stepcloseloop_Kp_low;	/**< \english Position feedback in the low-speed zone, range [0, 65535], default value 1000. \endenglish \russian Обратная связь по позиции в зоне малых скоростей, диапазон [0, 65535], значение по умолчанию 1000. \endrussian */
		unsigned int stepcloseloop_Kp_high;	/**< \english Position feedback in the high-speed zone, range [0, 65535], default value 33. \endenglish \russian Обратная связь по позиции в зоне больших скоростей, диапазон [0, 65535], значение по умолчанию 33. \endrussian */
	} engine_advansed_setup_t;

/**  
	* \english
	* EST settings
	* This data is stored in the controller's flash memory.
	* This structure is designed for the future. Currently, it is not in use.
	* \endenglish
	* \russian
	* Настройки EST.
	* Эти данные хранятся во flash памяти контроллера.
	* Эта структура на будущее. В настоящее время не используется.
	* \endrussian
	* @see set_extended_settings
	* @see get_extended_settings
	* @see get_extended_settings, set_extended_settings
	*/
	typedef struct
	{
		unsigned int Param1;	/**< \english \endenglish \russian \endrussian */
	} extended_settings_t;

/** 
	* \english
	* Position information.
	*
	* A useful structure that contains position value in steps and microsteps for stepper motor and encoder steps for all engines.
	* \endenglish
	* \russian
	* Данные о позиции.
	* Структура содержит значение положения в шагах и
	* микрошагах для шагового двигателя и в шагах энкодера всех
	* двигателей.
	* \endrussian
	* @see get_position
	*/
	typedef struct
	{
		int Position;	/**< \english The position of the whole steps in the engine \endenglish \russian Позиция в основных шагах двигателя \endrussian */
		int uPosition;	/**< \english Microstep position is only used with stepper motors. Microstep size and the range of valid values for this field depend on the selected step division mode (see MicrostepMode field in engine_settings). \endenglish \russian Позиция в микрошагах (используется только с шаговыми двигателями). Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
		long_t EncPosition;	/**< \english Encoder position.  \endenglish \russian Позиция энкодера. \endrussian */
	} get_position_t;

/** 
	* \english
	* Position information.
	*
	* A useful structure that contains position value in user units for stepper motor and encoder steps for all engines.
	* \endenglish
	* \russian
	* Данные о позиции.
	* Структура содержит значение положения в пользовательских единицах для шагового двигателя и в шагах энкодера всех
	* двигателей.
	* \endrussian
	* @see get_position
	*/
	typedef struct
	{
		float Position;	/**< \english The position in the engine. Corrected by the table. \endenglish \russian Позиция двигателя. Корректируется таблицей. \endrussian */
		long_t EncPosition;	/**< \english Encoder position.  \endenglish \russian Позиция энкодера. \endrussian */
	} get_position_calb_t;

/** 
	* \english
	* Position information.
	*
	* A useful structure that contains position value in steps and microsteps for stepper motor and encoder steps for all engines.
	* \endenglish
	* \russian
	* Данные о позиции.
	* Структура содержит значение положения в шагах и микрошагах для шагового двигателя и в шагах энкодера всех двигателей.
	* \endrussian
	* @see set_position
	*/
	typedef struct
	{
		int Position;	/**< \english The position of the whole steps in the engine \endenglish \russian Позиция в основных шагах двигателя \endrussian */
		int uPosition;	/**< \english Microstep position is only used with stepper motors. Microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). \endenglish \russian Позиция в микрошагах (используется только с шаговыми двигателями). Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). \endrussian */
		long_t EncPosition;	/**< \english Encoder position.  \endenglish \russian Позиция энкодера. \endrussian */
		unsigned int PosFlags;	/**< \english \ref flagset_positionflags "Position setting flags". \endenglish \russian \ref flagset_positionflags "Флаги установки положения". \endrussian */
	} set_position_t;

/** 
	* \english
	* User unit position information.
	*
	* A useful structure that contains position value in steps and microsteps for stepper motor and encoder steps of all engines.
	* \endenglish
	* \russian
	* Данные о позиции с использованием пользовательских единиц.
	* Структура содержит значение положения в шагах и микрошагах для шагового двигателя и в шагах энкодера всех двигателей.
	* \endrussian
	* @see set_position
	*/
	typedef struct
	{
		float Position;	/**< \english The position in the engine. \endenglish \russian Позиция двигателя. \endrussian */
		long_t EncPosition;	/**< \english Encoder position.  \endenglish \russian Позиция энкодера. \endrussian */
		unsigned int PosFlags;	/**< \english \ref flagset_positionflags "Position setting flags". \endenglish \russian \ref flagset_positionflags "Флаги установки положения". \endrussian */
	} set_position_calb_t;

/** 
	* \english
	* Device state.
	*
	* A useful structure that contains current controller state, including speed, position, and boolean flags.
	* \endenglish
	* \russian
	* Состояние устройства.
	* Эта структура содержит основные параметры текущего состоянии контроллера такие как скорость, позиция и флаги состояния.
	* \endrussian
	* @see get_status_impl
	*/
	typedef struct
	{
		unsigned int MoveSts;	/**< \english \ref flagset_movestate "Flags of move state". \endenglish \russian \ref flagset_movestate "Флаги состояния движения". \endrussian */
		unsigned int MvCmdSts;	/**< \english \ref flagset_mvcmdstatus "Move command state". \endenglish \russian \ref flagset_mvcmdstatus "Состояние команды движения". \endrussian */
		unsigned int PWRSts;	/**< \english \ref flagset_powerstate "Flags of power state of stepper motor". \endenglish \russian \ref flagset_powerstate "Флаги состояния питания шагового мотора". \endrussian */
		unsigned int EncSts;	/**< \english \ref flagset_encodestatus "Encoder state". \endenglish \russian \ref flagset_encodestatus "Состояние энкодера". \endrussian */
		unsigned int WindSts;	/**< \english \ref flagset_windstatus "Winding state". \endenglish \russian \ref flagset_windstatus "Состояние обмоток". \endrussian */
		int CurPosition;	/**< \english Current position. \endenglish \russian Первичное поле, в котором хранится текущая позиция, как бы ни была устроена обратная связь. В случае работы с DC-мотором в этом поле находится текущая позиция по данным с энкодера, в случае работы с ШД-мотором в режиме, когда первичными являются импульсы, подаваемые на мотор, в этом поле содержится целое значение шагов текущей позиции. \endrussian */
		int uCurPosition;	/**< \english Step motor shaft position in microsteps. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). Used with stepper motors only. \endenglish \russian Дробная часть текущей позиции в микрошагах. Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). Используется только с шаговым двигателем. \endrussian */
		long_t EncPosition;	/**< \english Current encoder position. \endenglish \russian Текущая позиция по данным с энкодера в импульсах энкодера, используется только если энкодер установлен, активизирован и не является основным датчиком положения, например при использовании энкодера совместно с шаговым двигателем для контроля проскальзования. \endrussian */
		int CurSpeed;	/**< \english Motor shaft speed in steps/s or rpm. \endenglish \russian Текущая скорость. \endrussian */
		int uCurSpeed;	/**< \english Fractional part of motor shaft speed in microsteps. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings). Used with stepper motors only. \endenglish \russian Дробная часть текущей скорости в микрошагах. Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings). Используется только с шаговым двигателем. \endrussian */
		int Ipwr;	/**< \english Engine current, mA. \endenglish \russian Ток потребления силовой части, мА. \endrussian */
		int Upwr;	/**< \english Power supply voltage, tens of mV. \endenglish \russian Напряжение на силовой части, десятки мВ. \endrussian */
		int Iusb;	/**< \english USB current, mA. \endenglish \russian Ток потребления по USB, мА. \endrussian */
		int Uusb;	/**< \english USB voltage, tens of mV. \endenglish \russian Напряжение на USB, десятки мВ. \endrussian */
		int CurT;	/**< \english Temperature, tenths of degrees Celsius. \endenglish \russian Температура процессора в десятых долях градусов Цельсия. \endrussian */
		unsigned int Flags;	/**< \english \ref flagset_stateflags "Status flags". \endenglish \russian \ref flagset_stateflags "Флаги состояния". \endrussian */
		unsigned int GPIOFlags;	/**< \english \ref flagset_gpioflags "Status flags of the GPIO outputs". \endenglish \russian \ref flagset_gpioflags "Флаги состояния GPIO входов". \endrussian */
		unsigned int CmdBufFreeSpace;	/**< \english This field is a service field. It shows the number of free synchronization chain buffer cells. \endenglish \russian Данное поле служебное. Оно показывает количество свободных ячеек буфера цепочки синхронизации. \endrussian */
	} status_t;

/** 
	* \english
	* User unit device's state.
	*
	* A useful structure that contains current controller state, including speed, position, and boolean flags.
	* \endenglish
	* \russian
	* Состояние устройства с использованием пользовательских единиц.
	* Эта структура содержит основные параметры текущего состоянии контроллера такие как скорость, позиция и флаги состояния.
	* \endrussian
	* @see get_status_impl
	*/
	typedef struct
	{
		unsigned int MoveSts;	/**< \english \ref flagset_movestate "Flags of move state". \endenglish \russian \ref flagset_movestate "Флаги состояния движения". \endrussian */
		unsigned int MvCmdSts;	/**< \english \ref flagset_mvcmdstatus "Move command state". \endenglish \russian \ref flagset_mvcmdstatus "Состояние команды движения". \endrussian */
		unsigned int PWRSts;	/**< \english \ref flagset_powerstate "Flags of power state of stepper motor". \endenglish \russian \ref flagset_powerstate "Флаги состояния питания шагового мотора". \endrussian */
		unsigned int EncSts;	/**< \english \ref flagset_encodestatus "Encoder state". \endenglish \russian \ref flagset_encodestatus "Состояние энкодера". \endrussian */
		unsigned int WindSts;	/**< \english \ref flagset_windstatus "Winding state". \endenglish \russian \ref flagset_windstatus "Состояние обмоток". \endrussian */
		float CurPosition;	/**< \english Current position. Corrected by the table. \endenglish \russian Первичное поле, в котором хранится текущая позиция, как бы ни была устроена обратная связь. В случае работы с DC-мотором в этом поле находится текущая позиция по данным с энкодера, в случае работы с ШД-мотором в режиме, когда первичными являются импульсы, подаваемые на мотор. Корректируется таблицей. \endrussian */
		long_t EncPosition;	/**< \english Current encoder position. \endenglish \russian Текущая позиция по данным с энкодера в импульсах энкодера, используется только если энкодер установлен, активизирован и не является основным датчиком положения, например при использовании энкодера совместно с шаговым двигателем для контроля проскальзования. \endrussian */
		float CurSpeed;	/**< \english Motor shaft speed. \endenglish \russian Текущая скорость. \endrussian */
		int Ipwr;	/**< \english Engine current, mA. \endenglish \russian Ток потребления силовой части, мА. \endrussian */
		int Upwr;	/**< \english Power supply voltage, tens of mV. \endenglish \russian Напряжение на силовой части, десятки мВ. \endrussian */
		int Iusb;	/**< \english USB current, mA. \endenglish \russian Ток потребления по USB, мА. \endrussian */
		int Uusb;	/**< \english USB voltage, tens of mV. \endenglish \russian Напряжение на USB, десятки мВ. \endrussian */
		int CurT;	/**< \english Temperature, tenths of degrees Celsius. \endenglish \russian Температура процессора в десятых долях градусов Цельсия. \endrussian */
		unsigned int Flags;	/**< \english \ref flagset_stateflags "Status flags". \endenglish \russian \ref flagset_stateflags "Флаги состояния". \endrussian */
		unsigned int GPIOFlags;	/**< \english \ref flagset_gpioflags "Status flags of the GPIO outputs". \endenglish \russian \ref flagset_gpioflags "Флаги состояния GPIO входов". \endrussian */
		unsigned int CmdBufFreeSpace;	/**< \english This field is a service field. It shows the number of free synchronization chain buffer cells. \endenglish \russian Данное поле служебное. Оно показывает количество свободных ячеек буфера цепочки синхронизации. \endrussian */
	} status_calb_t;

/** 
	* \english
	* The structure contains a sequence of measured axis motion parameters - velocities and position errors. The time step between consecutive measurements is 1 ms.
	* \endenglish
	* \russian
	* Структура содержит последовательность измеренных параметров движения оси – скоростей и ошибок по позиции. Шаг по времени между двумя последовательными измерениями - 1 мс.
	* \endrussian
	* @see measurements
	* @see get_measurements
	*/
	typedef struct
	{
		int Speed[25];	/**< \english Sequence of measured speeds (in encoder counts/s or microsteps/sec, depending on the motor type and control mode) \endenglish \russian Последовательность измеренных скоростей (в отсчётах энкодера/сек или микрошагах/сек, в зависимости от типа двигателя и режима управления) \endrussian */
		int Error[25];	/**< \english Position error in microsteps (whole steps are recalculated considering the current step division mode) or encoder counts. \endenglish \russian Последовательность измеренных ошибок позиции (в отсчётах энкодера или микрошагах, в зависимости от типа двигателя и режима управления) \endrussian */
		unsigned int Length;	/**< \english Actual sequence length. The values contained in cells Length, Length+1, ...24 shall not be interpreted as measurement results. \endenglish \russian Фактическая длина последовательности. Значения, содержащиеся в ячейках Length, Length + 1, ...24, не должны интерпретироваться в качестве результатов измерений. \endrussian */
	} measurements_t;

/** 
	* \english
	* Additional device state.
	*
	* This structure contains additional values such as winding's voltages, currents, and temperature.
	* \endenglish
	* \russian
	* Дополнительное состояние устройства.
	* Эта структура содержит основные дополнительные параметры текущего состоянии контроллера, такие напряжения и токи обмоток и температуру.
	* \endrussian
	* @see get_chart_data
	* @see get_chart_data
	*/
	typedef struct
	{
		int WindingVoltageA;	/**< \english In case of a step motor, it contains the voltage across the winding A (in tens of mV); in case of a brushless motor, it contains the voltage on the first coil; in case of a DC motor, it contains the only winding current. \endenglish \russian В случае ШД, напряжение на обмотке A (в десятках мВ); в случае бесщеточного, напряжение на первой обмотке; в случае DC - на единственной. \endrussian */
		int WindingVoltageB;	/**< \english In case of a step motor, it contains the voltage across the winding B (in tens of mV); in case of a brushless motor, it contains the voltage on the second winding; and in case of a DC motor, this field is not used. \endenglish \russian В случае ШД, напряжение на обмотке B (в десятках мВ); в случае бесщеточного, напряжение на второй обмотке; в случае DC не используется. \endrussian */
		int WindingVoltageC;	/**< \english In case of a brushless motor, it contains the voltage on the third winding (in tens of mV); in the case of a step motor and a DC motor, the field is not used. \endenglish \russian В случае бесщеточного, напряжение на третьей обмотке (в десятках мВ); в случае ШД и DC не используется. \endrussian */
		int WindingCurrentA;	/**< \english In case of a step motor, it contains the current in the winding A (in mA); in case of a brushless motor, it contains the current in the winding A; and in case of a DC motor, it contains the only winding current. \endenglish \russian В случае ШД, ток в обмотке A (в мA); в случае бесщеточного, ток в первой обмотке; в случае DC в единственной. \endrussian */
		int WindingCurrentB;	/**< \english In case of a step motor, it contains the current in the winding B (in mA); in case of a brushless motor, it contains the current in the winding B; and in case of a DC motor, the field is not used. \endenglish \russian В случае ШД, ток в обмотке B (в мА); в случае бесщеточного, ток в второй обмотке; в случае DC не используется. \endrussian */
		int WindingCurrentC;	/**< \english In case of a brushless motor, it contains the current in the winding C (in mA); in case of a step motor and a DC motor, the field is not used. \endenglish \russian В случае бесщеточного, ток в третьей обмотке (в мА); в случае ШД и DC не используется. \endrussian */
		unsigned int Pot;	/**< \english Analog input value, dimensionless. Range: 0..10000 \endenglish \russian Значение на аналоговом входе. Диапазон: 0..10000 \endrussian */
		unsigned int Joy;	/**< \english The joystick position, dimensionless. Range: 0..10000 \endenglish \russian Положение джойстика в десятитысячных долях. Диапазон: 0..10000 \endrussian */
		int AveragedPowerRatio;	/**< \english The ratio of motor supplied power to nominal motor power, as a percentage. \endenglish \russian Отношение подаваемой на мотор мощности к номинальной мощности, в процентах. \endrussian */
	} chart_data_t;

/** 
	* \english
	* Controller information structure.
	* \endenglish
	* \russian
	* Информации о контроллере.
	* \endrussian
	* @see get_device_information
	* @see get_device_information_impl
	*/
	typedef struct
	{
		char Manufacturer[5];	/**< \english Manufacturer \endenglish \russian Производитель \endrussian */
		char ManufacturerId[3];	/**< \english Manufacturer id \endenglish \russian Идентификатор производителя \endrussian */
		char ProductDescription[9];	/**< \english Product description \endenglish \russian Описание продукта \endrussian */
		unsigned int Major;	/**< \english The major number of the hardware version. \endenglish \russian Основной номер версии железа. \endrussian */
		unsigned int Minor;	/**< \english The minor number of the hardware version. \endenglish \russian Второстепенный номер версии железа. \endrussian */
		unsigned int Release;	/**< \english Release version. \endenglish \russian Номер правок этой версии железа. \endrussian */
	} device_information_t;

/**  
	* \english
	* The structure contains a new serial number, hardware version, and valid key. The SN and hardware version are changed and saved when the transmitted key matches the stored key. It can be used by the manufacturer only.
	* \endenglish
	* \russian
	* Структура с серийным номером и версией железа.
	* Вместе с новым серийным номером и версией железа передаётся "Ключ", только при совпадении которого происходит изменение и сохранение. Функция используется только производителем.
	* \endrussian
	* @see set_serial_number
	*/
	typedef struct
	{
		unsigned int SN;	/**< \english New board serial number. \endenglish \russian Новый серийный номер платы. \endrussian */
		uint8_t Key[32];	/**< \english Protection key (256 bit). \endenglish \russian Ключ защиты для установки серийного номера (256 бит). \endrussian */
		unsigned int Major;	/**< \english The major number of the hardware version. \endenglish \russian Основной номер версии железа. \endrussian */
		unsigned int Minor;	/**< \english The minor number of the hardware version. \endenglish \russian Второстепенный номер версии железа. \endrussian */
		unsigned int Release;	/**< \english Number of edits this release of hardware. \endenglish \russian Номер правок этой версии железа. \endrussian */
	} serial_number_t;

/** 
	* \english
	* Analog data.
	*
	* This structure contains raw analog data from the embedded ADC. These data are used for device testing and deep recalibration by the manufacturer only.
	* \endenglish
	* \russian
	* Аналоговые данные.
	* Эта структура содержит необработанные данные с АЦП и нормированные значения. Эти данные используются в сервисных целях для тестирования и калибровки устройства.
	* \endrussian
	* @see get_analog_data
	* @see get_analog_data
	*/
	typedef struct
	{
		unsigned int A1Voltage_ADC;	/**< \english "Voltage on pin 1 winding A" raw data from ADC. \endenglish \russian "Выходное напряжение на 1 выводе обмотки А" необработанные данные с АЦП. \endrussian */
		unsigned int A2Voltage_ADC;	/**< \english "Voltage on pin 2 winding A" raw data from ADC. \endenglish \russian "Выходное напряжение на 2 выводе обмотки А" необработанные данные с АЦП. \endrussian */
		unsigned int B1Voltage_ADC;	/**< \english "Voltage on pin 1 winding B" raw data from ADC. \endenglish \russian "Выходное напряжение на 1 выводе обмотки B" необработанные данные с АЦП. \endrussian */
		unsigned int B2Voltage_ADC;	/**< \english "Voltage on pin 2 winding B" raw data from ADC. \endenglish \russian "Выходное напряжение на 2 выводе обмотки B" необработанные данные с АЦП. \endrussian */
		unsigned int SupVoltage_ADC;	/**< \english "Supply voltage of H-bridge's MOSFETs" raw data from ADC. \endenglish \russian "Напряжение питания ключей Н-моста" необработанные данные с АЦП. \endrussian */
		unsigned int ACurrent_ADC;	/**< \english "Winding A current" raw data from ADC. \endenglish \russian "Ток через обмотку А" необработанные данные с АЦП. \endrussian */
		unsigned int BCurrent_ADC;	/**< \english "Winding B current" raw data from ADC. \endenglish \russian "Ток через обмотку B" необработанные данные с АЦП. \endrussian */
		unsigned int FullCurrent_ADC;	/**< \english "Full current" raw data from ADC. \endenglish \russian "Полный ток" необработанные данные с АЦП. \endrussian */
		unsigned int Temp_ADC;	/**< \english Voltage from temperature sensor, raw data from ADC. \endenglish \russian Напряжение с датчика температуры, необработанные данные с АЦП. \endrussian */
		unsigned int Joy_ADC;	/**< \english Joystick raw data from ADC. \endenglish \russian Джойстик, необработанные данные с АЦП. \endrussian */
		unsigned int Pot_ADC;	/**< \english Voltage on analog input, raw data from ADC \endenglish \russian Напряжение на аналоговом входе, необработанные данные с АЦП \endrussian */
		unsigned int Enc_Check_ADC;	/**< \english Voltage on encoder check line, raw ADC data. Used to determine encoder type: single-ended or differential. \endenglish \russian Напряжение на линии проверки типа энкодера, необработанные данные АЦП. Используется для определения типа энкодера: с однофазным выходом или дифференциальный. \endrussian */
		unsigned int deprecated0;
		int A1Voltage;	/**< \english "Voltage on pin 1 winding A" calibrated data (in tens of mV). \endenglish \russian "Выходное напряжение на 1 выводе обмотки А" откалиброванные данные (в десятках мВ). \endrussian */
		int A2Voltage;	/**< \english "Voltage on pin 2 winding A" calibrated data (in tens of mV). \endenglish \russian "Выходное напряжение на 2 выводе обмотки А" откалиброванные данные (в десятках мВ). \endrussian */
		int B1Voltage;	/**< \english "Voltage on pin 1 winding B" calibrated data (in tens of mV). \endenglish \russian "Выходное напряжение на 1 выводе обмотки B" откалиброванные данные (в десятках мВ). \endrussian */
		int B2Voltage;	/**< \english "Voltage on pin 2 winding B" calibrated data (in tens of mV). \endenglish \russian "Выходное напряжение на 2 выводе обмотки B" откалиброванные данные (в десятках мВ). \endrussian */
		int SupVoltage;	/**< \english "Supply voltage on the top of H-bridge's MOSFETs" calibrated data (in tens of mV). \endenglish \russian "Напряжение питания ключей Н-моста" откалиброванные данные (в десятках мВ). \endrussian */
		int ACurrent;	/**< \english "Winding A current" calibrated data (in mA). \endenglish \russian "Ток через обмотку А" откалиброванные данные (в мА). \endrussian */
		int BCurrent;	/**< \english "Winding B current" calibrated data (in mA). \endenglish \russian "Ток через обмотку B" откалиброванные данные (в мА). \endrussian */
		int FullCurrent;	/**< \english "Full current" calibrated data (in mA). \endenglish \russian "Полный ток" откалиброванные данные (в мА). \endrussian */
		int Temp;	/**< \english Temperature, calibrated data (in tenths of degrees Celsius). \endenglish \russian Температура, откалиброванные данные (в десятых долях градуса Цельсия). \endrussian */
		int Joy;	/**< \english Joystick, calibrated data. Range: 0..10000 \endenglish \russian Джойстик во внутренних единицах. Диапазон: 0..10000 \endrussian */
		int Pot;	/**< \english Analog input, calibrated data. Range: 0..10000 \endenglish \russian Аналоговый вход во внутренних единицах. Диапазон: 0..10000 \endrussian */
		int Enc_Check;	/**< \english Voltage on encoder check line, exponentially filtrated ADC codes. Used to determine encoder type: single-ended or differential. \endenglish \russian Напряжение на линии проверки типа энкодера, экспонениально сглаженные данные АЦП. Используется для определения типа энкодера: с однофазным выходом или дифференциальный. \endrussian */
		unsigned int deprecated1[2];
		int R;	/**< \english Motor winding resistance in mOhms (is only used with stepper motors). \endenglish \russian Сопротивление обмоток двигателя(для шагового двигателя),  в мОм \endrussian */
		int L;	/**< \english Motor winding pseudo inductance in uH (is only used with stepper motors). \endenglish \russian Псевдоиндуктивность обмоток двигателя(для шагового двигателя),  в мкГн \endrussian */
	} analog_data_t;

/** 
	* \english
	* Debug data.
	* These data are used for device debugging by the manufacturer.
	* \endenglish
	* \russian
	* Отладочные данные.
	* Эти данные используются в сервисных целях для тестирования и отладки устройства.
	* \endrussian
	* @see get_debug_read
	*/
	typedef struct
	{
		uint8_t DebugData[128];	/**< \english Arbitrary debug data. \endenglish \russian Отладочные данные. \endrussian */
	} debug_read_t;

/** 
	* \english
	* Debug data.
	* These data are used for device debugging by the manufacturer.
	* \endenglish
	* \russian
	* Отладочные данные.
	* Эти данные используются в сервисных целях для тестирования и отладки устройства.
	* \endrussian
	* @see set_debug_write
	*/
	typedef struct
	{
		uint8_t DebugData[128];	/**< \english Arbitrary debug data. \endenglish \russian Отладочные данные. \endrussian */
	} debug_write_t;

/** 
	* \english
	* Stage username.
	* \endenglish
	* \russian
	* Пользовательское имя подвижки.
	* \endrussian
	* @see get_stage_name, set_stage_name
	*/
	typedef struct
	{
		char PositionerName[17];	/**< \english User's positioner name. It can be set by a user. Max string length: 16 characters. \endenglish \russian Пользовательское имя подвижки. Может быть установлено пользователем для его удобства. Максимальная длина строки: 16 символов. \endrussian */
	} stage_name_t;

/** 
	* \english
	* Deprecated. Stage information. Deprecated.
	* \endenglish
	* \russian
	* Информация о позиционере.
	* \endrussian
	* @see set_stage_information
	* @see get_stage_information
	* @see get_stage_information, set_stage_information
	*/
	typedef struct
	{
		char Manufacturer[17];	/**< \english Manufacturer. Max string length: 16 chars. \endenglish \russian Производитель. Максимальная длина строки: 16 символов. \endrussian */
		char PartNumber[25];	/**< \english Series and PartNumber. Max string length: 24 chars. \endenglish \russian Серия и номер модели. Максимальная длина строки: 24 символа. \endrussian */	
	} stage_information_t;

/** 
	* \english
	* Deprecated. Stage settings.
	* \endenglish
	* \russian
	* Настройки позиционера.
	* \endrussian
	* @see set_stage_settings
	* @see get_stage_settings
	* @see get_stage_settings, set_stage_settings
	*/
	typedef struct
	{
		float LeadScrewPitch;	/**< \english Lead screw pitch (mm). Data type: float. \endenglish \russian Шаг ходового винта в мм. Тип данных: float. \endrussian */	
		char Units[9];	/**< \english Units for MaxSpeed and TravelRange fields of the structure (steps, degrees, mm, ...). Max string length: 8 chars. \endenglish \russian Единицы измерения расстояния, используемые в полях MaxSpeed и TravelRange (шаги, градусы, мм, ...), Максимальная длина строки: 8 символов. \endrussian */	
		float MaxSpeed;	/**< \english Maximum speed (Units/c). Data type: float. \endenglish \russian Максимальная скорость (Units/с). Тип данных: float. \endrussian */	
		float TravelRange;	/**< \english Travel range (Units). Data type: float. \endenglish \russian Диапазон перемещения (Units). Тип данных: float. \endrussian */	
		float SupplyVoltageMin;	/**< \english Minimum supply voltage (V). Data type: float. \endenglish \russian Минимальное напряжение питания (В). Тип данных: float. \endrussian */	
		float SupplyVoltageMax;	/**< \english Maximum supply voltage (V). Data type: float. \endenglish \russian Максимальное напряжение питания (В). Тип данных: float. \endrussian */	
		float MaxCurrentConsumption;	/**< \english Maximum current consumption (A). Data type: float. \endenglish \russian Максимальный ток потребления (А). Тип данных: float. \endrussian */	
		float HorizontalLoadCapacity;	/**< \english Horizontal load capacity (kg). Data type: float. \endenglish \russian Горизонтальная грузоподъемность (кг). Тип данных: float. \endrussian */	
		float VerticalLoadCapacity;	/**< \english Vertical load capacity (kg). Data type: float. \endenglish \russian Вертикальная грузоподъемность (кг). Тип данных: float. \endrussian */	
	} stage_settings_t;

/** 
	* \english
	* Deprecated. motor information.
	* \endenglish
	* \russian
	* Информация о двигателе.
	* \endrussian
	* @see set_motor_information
	* @see get_motor_information
	* @see get_motor_information, set_motor_information
	*/
	typedef struct
	{
		char Manufacturer[17];	/**< \english Manufacturer. Max string length: 16 chars. \endenglish \russian Производитель. Максимальная длина строки: 16 символов. \endrussian */	
		char PartNumber[25];	/**< \english Series and PartNumber. Max string length: 24 chars. \endenglish \russian Серия и номер модели. Максимальная длина строки: 24 символа. \endrussian */	
	} motor_information_t;

/** 
	* \english
	* Deprecated. Physical characteristics and limitations of the motor.
	* \endenglish
	* \russian
	* Физический характеристики и ограничения мотора.
	* \endrussian
	* @see set_motor_settings
	* @see get_motor_settings
	* @see get_motor_settings, set_motor_settings
	*/
	typedef struct
	{
		unsigned int MotorType;	/**< \english \ref flagset_motortypeflags "Motor Type flags". \endenglish \russian \ref flagset_motortypeflags "Флаги типа двигателя". \endrussian */
		unsigned int ReservedField;	/**< \english Reserved \endenglish \russian Зарезервировано \endrussian */
		unsigned int Poles;	/**< \english Number of pole pairs for DC or BLDC motors or number of steps per rotation for stepper motors. \endenglish \russian Кол-во пар полюсов у DС или BLDC двигателя или кол-во шагов на оборот для шагового двигателя. \endrussian */
		unsigned int Phases;	/**< \english Number of phases for BLDC motors. \endenglish \russian Кол-во фаз у BLDC двигателя. \endrussian */
		float NominalVoltage;	/**< \english Nominal voltage on winding (B). Data type: float \endenglish \russian Номинальное напряжение на обмотке (В). Тип данных: float. \endrussian */
		float NominalCurrent;	/**< \english Maximum direct current in winding for DC and BLDC engines, nominal current in windings for stepper motors (A). Data type: float.  \endenglish \russian Максимальный постоянный ток в обмотке для DC и BLDC двигателей, номинальный ток в обмотке для шаговых двигателей (А). Тип данных: float. \endrussian */
		float NominalSpeed;	/**< \english Not used. Nominal speed (rpm). Used for DC and BLDC engines. Data type: float. \endenglish \russian Не используется. Номинальная скорость (об/мин). Применяется для DC и BLDC двигателей. Тип данных: float. \endrussian */
		float NominalTorque;	/**< \english Nominal torque (mN * m). Used for DC and BLDC engines. Data type: float. \endenglish \russian Номинальный крутящий момент (мН * м). Применяется для DC и BLDC двигателей. Тип данных: float. \endrussian */
		float NominalPower;	/**< \english Nominal power (W). Used for DC and BLDC engines. Data type: float. \endenglish \russian Номинальная мощность(Вт). Применяется для DC и BLDC двигателей. Тип данных: float. \endrussian */	
		float WindingResistance;	/**< \english Resistance of windings for DC engines, of each of two windings for stepper motors, or of each of three windings for BLDC engines (Ohm). Data type: float.\endenglish \russian Сопротивление обмотки DC двигателя, каждой из двух обмоток шагового двигателя или каждой из трёх обмоток BLDC двигателя (Ом). Тип данных: float. \endrussian */
		float WindingInductance;	/**< \english Inductance of windings for DC engines, inductance of each of two windings for stepper motors, or inductance of each of three windings for BLDC engines (mH). Data type: float.\endenglish \russian Индуктивность обмотки DC двигателя, каждой из двух обмоток шагового двигателя или каждой из трёх обмоток BLDC двигателя (мГн). Тип данных: float. \endrussian */
		float RotorInertia;	/**< \english Rotor inertia (g * cm2). Data type: float.\endenglish \russian Инерция ротора (г cм2). Тип данных: float. \endrussian */
		float StallTorque;	/**< \english Torque hold position for a stepper motor or torque at a motionless rotor for other types of engines (mN * m). Data type: float. \endenglish \russian Крутящий момент удержания позиции для шагового двигателя или крутящий момент при неподвижном роторе для других типов двигателей (мН м). Тип данных: float. \endrussian */
		float DetentTorque;	/**< \english Holding torque position with unpowered windings (mN * m). Data type: float. \endenglish \russian Момент удержания позиции с незапитанными обмотками (мН м). Тип данных: float. \endrussian */
		float TorqueConstant;	/**< \english Torque constant that determines the proportionality constant between the maximum rotor torque and current flowing in the winding (mN * m / A). Used mainly for DC motors. Data type: float. \endenglish \russian Константа крутящего момента, определяющая коэффициент пропорциональности максимального момента силы ротора от протекающего в обмотке тока (мН м/А). Используется в основном для DC двигателей. Тип данных: float. \endrussian */
		float SpeedConstant;	/**< \english Velocity constant, which determines the value or the amplitude of the induced voltage on the motion of DC or BLDC motors (rpm / V) or stepper motors (steps/s / V). Data type: float. \endenglish \russian Константа скорости, определяющая значение или амплитуду напряжения наведённой индукции при вращении ротора DC или BLDC двигателя (об/мин / В) или шагового двигателя (шаг/с / В). Тип данных: float. \endrussian */
		float SpeedTorqueGradient;	/**< \english Speed torque gradient (rpm / mN * m). Data type: float. \endenglish \russian Градиент крутящего момента (об/мин / мН м). Тип данных: float. \endrussian */
		float MechanicalTimeConstant;	/**< \english Mechanical time constant (ms). Data type: float. \endenglish \russian Механическая постоянная времени (мс). Тип данных: float. \endrussian */
		float MaxSpeed;	/**< \english The maximum speed for stepper motors (steps/s) or DC and BLDC motors (rmp). Data type: float. \endenglish \russian Максимальная разрешённая скорость для шаговых двигателей (шаг/с) или для DC и BLDC двигателей (об/мин). Тип данных: float. \endrussian */
		float MaxCurrent;	/**< \english The maximum current in the winding (A). Data type: float. \endenglish \russian Максимальный ток в обмотке (А). Тип данных: float. \endrussian */
		float MaxCurrentTime;	/**< \english Safe duration of overcurrent in the winding (ms). Data type: float. \endenglish \russian Безопасная длительность максимального тока в обмотке (мс). Тип данных: float. \endrussian */
		float NoLoadCurrent;	/**< \english The current consumption in idle mode (A). Used for DC and BLDC motors. Data type: float. \endenglish \russian Ток потребления в холостом режиме (А). Применяется для DC и BLDC двигателей. Тип данных: float. \endrussian */
		float NoLoadSpeed;	/**< \english Idle speed (rpm). Used for DC and BLDC motors. Data type: float. \endenglish \russian Скорость в холостом режиме (об/мин). Применяется для DC и BLDC двигателей. Тип данных: float. \endrussian */
	} motor_settings_t;

/** 
	* \english
	* Deprecated. Encoder information.
	* \endenglish
	* \russian
	* Информация об энкодере.
	* \endrussian
	* @see set_encoder_information
	* @see get_encoder_information
	* @see get_encoder_information, set_encoder_information
	*/
	typedef struct
	{
		char Manufacturer[17];	/**< \english Manufacturer. Max string length: 16 chars. \endenglish \russian Производитель. Максимальная длина строки: 16 символов. \endrussian */	
		char PartNumber[25];	/**< \english Series and PartNumber. Max string length: 24 chars. \endenglish \russian Серия и номер модели. Максимальная длина строки: 24 символа. \endrussian */	
	} encoder_information_t;

/** 
	* \english
	* Deprecated. Encoder settings.
	* \endenglish
	* \russian
	* Настройки энкодера.
	* \endrussian
	* @see set_encoder_settings
	* @see get_encoder_settings
	* @see get_encoder_settings, set_encoder_settings
	*/
	typedef struct
	{
		float MaxOperatingFrequency;	/**< \english Maximum operation frequency (kHz). Data type: float. \endenglish \russian Максимальная частота (кГц). Тип данных: float. \endrussian */
		float SupplyVoltageMin;	/**< \english Minimum supply voltage (V). Data type: float. \endenglish \russian Минимальное напряжение питания (В). Тип данных: float. \endrussian */
		float SupplyVoltageMax;	/**< \english Maximum supply voltage (V). Data type: float. \endenglish \russian Максимальное напряжение питания (В). Тип данных: float. \endrussian */
		float MaxCurrentConsumption;	/**< \english Max current consumption (mA). Data type: float. \endenglish \russian Максимальное потребление тока (мА). Тип данных: float. \endrussian */
		unsigned int PPR;	/**< \english The number of counts per revolution \endenglish \russian Количество отсчётов на оборот \endrussian */
		unsigned int EncoderSettings;	/**< \english \ref flagset_encodersettingsflags "Encoder settings flags". \endenglish \russian \ref flagset_encodersettingsflags "Флаги настроек энкодера". \endrussian */
	} encoder_settings_t;

/** 
	* \english
	* Deprecated. Hall sensor information.
	* \endenglish
	* \russian
	* Информация о датчиках Холла.
	* \endrussian
	* @see set_hallsensor_information
	* @see get_hallsensor_information
	* @see get_hallsensor_information, set_hallsensor_information
	*/
	typedef struct
	{
		char Manufacturer[17];	/**< \english Manufacturer. Max string length: 16 chars. \endenglish \russian Производитель. Максимальная длина строки: 16 символов. \endrussian */	
		char PartNumber[25];	/**< \english Series and PartNumber. Max string length: 24 chars. \endenglish \russian Серия и номер модели. Максимальная длина строки: 24 символа. \endrussian */	
	} hallsensor_information_t;

/** 
	* \english
	* Deprecated. Hall sensor settings.
	* \endenglish
	* \russian
	* Настройки датчиков Холла.
	* \endrussian
	* @see set_hallsensor_settings
	* @see get_hallsensor_settings
	* @see get_hallsensor_settings, set_hallsensor_settings
	*/
	typedef struct
	{
		float MaxOperatingFrequency;	/**< \english Maximum operation frequency (kHz). Data type: float. \endenglish \russian Максимальная частота (кГц). Тип данных: float. \endrussian */
		float SupplyVoltageMin;	/**< \english Minimum supply voltage (V). Data type: float. \endenglish \russian Минимальное напряжение питания (В). Тип данных: float. \endrussian */
		float SupplyVoltageMax;	/**< \english Maximum supply voltage (V). Data type: float. \endenglish \russian Максимальное напряжение питания (В). Тип данных: float. \endrussian */
		float MaxCurrentConsumption;	/**< \english Maximum current consumption (mA). Data type: float. \endenglish \russian Максимальное потребление тока (мА). Тип данных: float. \endrussian */
		unsigned int PPR;	/**< \english The number of counts per revolution \endenglish \russian Количество отсчётов на оборот \endrussian */
	} hallsensor_settings_t;

/** 
	* \english
	* Deprecated. Gear information.
	* \endenglish
	* \russian
	* Информация о редукторе.
	* \endrussian
	* @see set_gear_information
	* @see get_gear_information
	* @see get_gear_information, set_gear_information
	*/
	typedef struct
	{
		char Manufacturer[17];	/**< \english Manufacturer. Max string length: 16 chars. \endenglish \russian Производитель. Максимальная длина строки: 16 символов. \endrussian */	
		char PartNumber[25];	/**< \english Series and PartNumber. Max string length: 24 chars. \endenglish \russian Серия и номер модели. Максимальная длина строки: 24 символа. \endrussian */	
	} gear_information_t;

/** 
	* \english
	* Deprecated. Gear settings.
	* \endenglish
	* \russian
	* Настройки редуктора.
	* \endrussian
	* @see set_gear_settings
	* @see get_gear_settings
	* @see get_gear_settings, set_gear_settings
	*/
	typedef struct
	{
		float ReductionIn;	/**< \english Input reduction coefficient. (Output = (ReductionOut / ReductionIn) * Input) Data type: float. \endenglish \russian Входной коэффициент редуктора. (Выход = (ReductionOut/ReductionIn) * вход) Тип данных: float. \endrussian */
		float ReductionOut;	/**< \english Output reduction coefficient. (Output = (ReductionOut / ReductionIn) * Input) Data type: float. \endenglish \russian Выходной коэффициент редуктора. (Выход = (ReductionOut/ReductionIn) * вход) Тип данных: float. \endrussian */
		float RatedInputTorque;	/**< \english Maximum continuous torque (N * m). Data type: float. \endenglish \russian Максимальный крутящий момент (Н * м). Тип данных: float. \endrussian */
		float RatedInputSpeed;	/**< \english Maximum speed on the input shaft (rpm). Data type: float. \endenglish \russian Максимальная скорость на входном валу редуктора (об/мин). Тип данных: float. \endrussian */
		float MaxOutputBacklash;	/**< \english Output backlash of the reduction gear (degree). Data type: float. \endenglish \russian Выходной люфт редуктора (градус). Тип данных: float.\endrussian */
		float InputInertia;	/**< \english Equivalent input gear inertia (g * cm2). Data type: float. \endenglish \russian Эквивалентная входная инерция редуктора(г * см2). Тип данных: float. \endrussian */
		float Efficiency;	/**< \english Reduction gear efficiency (%). Data type: float. \endenglish \russian КПД редуктора (%). Тип данных: float. \endrussian */
	} gear_settings_t;

/** 
	* \english
	* Deprecated. Additional accessories' information.
	* \endenglish
	* \russian
	* Информация о дополнительных аксессуарах.
	* \endrussian
	* @see set_accessories_settings
	* @see get_accessories_settings
	* @see get_accessories_settings, set_accessories_settings
	*/
	typedef struct
	{
		char MagneticBrakeInfo[25];	/**< \english The manufacturer and the part number of magnetic brake, the maximum string length is 24 characters. \endenglish \russian Производитель и номер магнитного тормоза, Максимальная длина строки: 24 символов. \endrussian */ 
		float MBRatedVoltage;	/**< \english Rated voltage for controlling the magnetic brake (V). Data type: float. \endenglish \russian Номинальное напряжение для управления магнитным тормозом (В). Тип данных: float. \endrussian */
		float MBRatedCurrent;	/**< \english Rated current for controlling the magnetic brake (A). Data type: float. \endenglish \russian Номинальный ток для управления магнитным тормозом (А). Тип данных: float. \endrussian */ 
		float MBTorque;	/**< \english Retention moment (mN * m). Data type: float. \endenglish \russian Удерживающий момент (мН * м). Тип данных: float. \endrussian */ 
		unsigned int MBSettings;	/**< \english \ref flagset_mbsettingsflags "Magnetic brake settings flags". \endenglish \russian \ref flagset_mbsettingsflags "Флаги настроек энкодера". \endrussian */
		char TemperatureSensorInfo[25];	/**< \english The manufacturer and the part number of the temperature sensor, the maximum string length: 24 characters. \endenglish \russian Производитель и номер температурного датчика, Максимальная длина строки: 24 символов. \endrussian */ 
		float TSMin;	/**< \english The minimum measured temperature (degrees Celsius) Data type: float. \endenglish \russian Минимальная измеряемая температура (град Цельсия). Тип данных: float. \endrussian */ 
		float TSMax;	/**< \english The maximum measured temperature (degrees Celsius) Data type: float. \endenglish \russian Максимальная измеряемая температура (град Цельсия) Тип данных: float. \endrussian */ 
		float TSGrad;	/**< \english The temperature gradient (V/degrees Celsius). Data type: float. \endenglish \russian Температурный градиент (В/град Цельсия). Тип данных: float. \endrussian */ 
		unsigned int TSSettings;	/**< \english \ref flagset_tssettingsflags "Temperature sensor settings flags". \endenglish \russian \ref flagset_tssettingsflags "Флаги настроек температурного датчика". \endrussian */
		unsigned int LimitSwitchesSettings;	/**< \english \ref flagset_lsflags "Temperature sensor settings flags". \endenglish \russian \ref flagset_lsflags "Флаги настроек температурного датчика". \endrussian */
	} accessories_settings_t;

/** 
	* \english
	* Random key. Manufacturer only.
	*
	* Structure that contains a random key. It is used in the encryption of WKEY and SSER command contents.
	* \endenglish
	* \russian
	* Случайный ключ. Только для производителя.
	* Структура которая содержит случайный ключ, использующийся для шифрования содержимого команд WKEY и SSER.
	* \endrussian
	* @see get_init_random
	*/
	typedef struct
	{
		uint8_t key[16];	/**< \english Random key. \endenglish \russian Случайный ключ. \endrussian */
	} init_random_t;

/** 
  * \english
  * Globally unique identifier. Manufacturer only.
  * \endenglish
  * \russian
  * Глобальный уникальный идентификатор. Только для производителя.
  * \endrussian
	* @see get_globally_unique_identifier
  */
	typedef struct
	{
		unsigned int UniqueID0;	/**< \english Unique ID 0. \endenglish \russian Уникальный ID 0. \endrussian */
		unsigned int UniqueID1;	/**< \english Unique ID 1. \endenglish \russian Уникальный ID 1. \endrussian */
		unsigned int UniqueID2;	/**< \english Unique ID 2. \endenglish \russian Уникальный ID 2. \endrussian */
		unsigned int UniqueID3;	/**< \english Unique ID 3. \endenglish \russian Уникальный ID 3. \endrussian */
	} globally_unique_identifier_t;

/*
 --------------------------------------------
   BEGIN OF GENERATED function declarations
 --------------------------------------------
*/

/**
	* \english
	* @name Controller settings setup
	* Read and write functions for almost all controller settings.
	* \endenglish
	* \russian
	* @name Группа команд настройки контроллера
	* Функции для чтения/записи большинства настроек контроллера.
	* \endrussian
	*/

//@{

/** 
	* \english
	* Feedback settings.
	* @param id An identifier of a device
	* @param[in] IPS number of encoder counts per shaft revolution. Range: 1..65535. The field is obsolete, it is recommended to write 0 to IPS and use the extended CountsPerTurn field. You may need to update the controller firmware to the latest version.
	* @param[in] FeedbackType type of feedback
	* @param[in] FeedbackFlags flags of feedback
	* @param[in] CountsPerTurn number of encoder counts per shaft revolution. Range: 1..4294967295. To use the CountsPerTurn field, write 0 in the IPS field, otherwise the value from the IPS field will be used.
	* \endenglish
	* \russian
	* Запись настроек обратной связи.
	* @param id идентификатор устройства
	* @param[in] IPS количество отсчётов энкодера на оборот вала. Диапазон: 1..65535. Поле устарело, рекомендуется записывать 0 в IPS и использовать расширенное поле CountsPerTurn. Может потребоваться обновление микропрограммы контроллера до последней версии.
	* @param[in] FeedbackType тип обратной связи
	* @param[in] FeedbackFlags флаги обратной связи
	* @param[in] CountsPerTurn количество отсчётов энкодера на оборот вала. Диапазон: 1..4294967295. Для использования поля CountsPerTurn нужно записать 0 в поле IPS, иначе будет использоваться значение из поля IPS.
	* \endrussian
	*/
	result_t XIMC_API set_feedback_settings (device_t id, const feedback_settings_t* feedback_settings);

/** 
	* \english
	* Feedback settings.
	* @param id An identifier of a device
	* @param[out] IPS number of encoder counts per shaft revolution. Range: 1..65535. The field is obsolete, it is recommended to write 0 to IPS and use the extended CountsPerTurn field. You may need to update the controller firmware to the latest version.
	* @param[out] FeedbackType type of feedback
	* @param[out] FeedbackFlags flags of feedback
	* @param[out] CountsPerTurn number of encoder counts per shaft revolution. Range: 1..4294967295. To use the CountsPerTurn field, write 0 in the IPS field, otherwise the value from the IPS field will be used.
	* \endenglish
	* \russian
	* Чтение настроек обратной связи
	* @param id идентификатор устройства
	* @param[out] IPS количество отсчётов энкодера на оборот вала. Диапазон: 1..65535. Поле устарело, рекомендуется записывать 0 в IPS и использовать расширенное поле CountsPerTurn. Может потребоваться обновление микропрограммы контроллера до последней версии.
	* @param[out] FeedbackType тип обратной связи
	* @param[out] FeedbackFlags флаги обратной связи
	* @param[out] CountsPerTurn количество отсчётов энкодера на оборот вала. Диапазон: 1..4294967295. Для использования поля CountsPerTurn нужно записать 0 в поле IPS, иначе будет использоваться значение из поля IPS.
	* \endrussian
	*/
	result_t XIMC_API get_feedback_settings (device_t id, feedback_settings_t* feedback_settings);

/** 
	* \english
	* Set home settings.
	* This function sends home position structure to the controller's memory.
	* @see home_settings_t
	* @param id An identifier of a device
	* @param[in] home_settings calibrating position settings
	* \endenglish
	* \russian
	* Команда записи настроек для подхода в home position.
	* Эта функция записывает структуру настроек, использующихся для калибровки позиции, в память контроллера.
	* @see home_settings_t
	* @param id идентификатор устройства
	* @param[out] home_settings настройки калибровки позиции
	* \endrussian
	*/
	result_t XIMC_API set_home_settings (device_t id, const home_settings_t* home_settings);

/** 
	* \english
	* Set user unit home settings.
	* This function sends home position structure to the controller's memory.
	* @see home_settings_calb_t
	* @param id An identifier of a device
	* @param[in] home_settings_calb calibrating position settings
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Команда записи настроек для подхода в home position с использованием пользовательских единиц.
	* Эта функция записывает структуру настроек, использующихся для калибровки позиции, в память контроллера.
	* @see home_settings_calb_t
	* @param id идентификатор устройства
	* @param[in] home_settings_calb настройки калибровки позиции
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API set_home_settings_calb (device_t id, const home_settings_calb_t* home_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Read home settings.
	* This function reads the structure with home position settings.
	* @see home_settings_t
	* @param id An identifier of a device
	* @param[out] home_settings calibrating position settings
	* \endenglish
	* \russian
	* Команда чтения настроек для подхода в home position.
	* Эта функция заполняет структуру настроек, использующихся для калибровки позиции, в память контроллера.
	* @see home_settings_t
	* @param id идентификатор устройства
	* @param[out] home_settings настройки калибровки позиции
	* \endrussian
	*/
	result_t XIMC_API get_home_settings (device_t id, home_settings_t* home_settings);

/** 
	* \english
	* Read user unit home settings.
	* This function reads the structure with home position settings.
	* @see home_settings_calb_t
	* @param id An identifier of a device
	* @param[out] home_settings_calb calibrating position settings
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Команда чтения настроек для подхода в home position с использованием пользовательских единиц.
	* Эта функция заполняет структуру настроек, использующихся для калибровки позиции, в память контроллера.
	* @see home_settings_calb_t
	* @param id идентификатор устройства
	* @param[out] home_settings_calb настройки калибровки позиции
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API get_home_settings_calb (device_t id, home_settings_calb_t* home_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Movement settings set command (speed, acceleration, threshold, etc.).
	* @param id An identifier of a device
	* @param[in] move_settings structure contains move settings: speed, acceleration, deceleration etc.
	* \endenglish
	* \russian
	* Команда записи настроек перемещения (скорость, ускорение, threshold и скорость в режиме антилюфта).
	* @param id идентификатор устройства
	* @param[in] move_settings структура, содержащая настройки движения: скорость, ускорение, и т.д.
	* \endrussian
	*/
	result_t XIMC_API set_move_settings (device_t id, const move_settings_t* move_settings);

/** 
	* \english
	* User unit movement settings set command (speed, acceleration, threshold, etc.).
	* @param id An identifier of a device
	* @param[in] move_settings_calb structure contains move settings: speed, acceleration, deceleration etc.
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Команда записи настроек перемещения, с использованием пользовательских единиц (скорость, ускорение, threshold и скорость в режиме антилюфта).
	* @param id идентификатор устройства
	* @param[in] move_settings_calb структура, содержащая настройки движения: скорость, ускорение, и т.д.
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API set_move_settings_calb (device_t id, const move_settings_calb_t* move_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Movement settings read command (speed, acceleration, threshold, etc.).
	* @param id An identifier of a device
	* @param[out] move_settings structure contains move settings: speed, acceleration, deceleration etc.
	* \endenglish
	* \russian
	* Команда чтения настроек перемещения (скорость, ускорение, threshold и скорость в режиме антилюфта).
	* @param id идентификатор устройства
	* @param[out] move_settings структура, содержащая настройки движения: скорость, ускорение, и т.д.
	* \endrussian
	*/
	result_t XIMC_API get_move_settings (device_t id, move_settings_t* move_settings);

/** 
	* \english
	* User unit movement settings read command (speed, acceleration, threshold, etc.).
	* @param id An identifier of a device
	* @param[out] move_settings_calb structure contains move settings: speed, acceleration, deceleration etc.
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Команда чтения настроек перемещения с использованием пользовательских единиц(скорость, ускорение, threshold и скорость в режиме антилюфта).
	* @param id идентификатор устройства
	* @param[out] move_settings_calb структура, содержащая настройки движения: скорость, ускорение, и т.д.
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API get_move_settings_calb (device_t id, move_settings_calb_t* move_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Set engine settings.
	* This function sends a structure with a set of engine settings to the controller's memory. These settings specify the motor shaft movement algorithm, list of limitations and rated characteristics. Use it when you change the motor, encoder, positioner, etc. Please note that wrong engine settings may lead to device malfunction, which can cause irreversible damage to the board.
	* @see get_engine_settings
	* @param id An identifier of a device
	* @param[in] engine_settings engine settings
	* \endenglish
	* \russian
	* Запись настроек мотора.
	* Настройки определяют номинальные значения напряжения, тока, скорости мотора, характер движения и тип мотора.
	* Пожалуйста, загружайте новые настройки когда вы меняете мотор, энкодер или позиционер.
	* Помните, что неправильные настройки мотора могут повредить оборудование.
	* @see get_engine_settings
	* @param id идентификатор устройства
	* @param[in] engine_settings структура с настройками мотора
	* \endrussian
	*/
	result_t XIMC_API set_engine_settings (device_t id, const engine_settings_t* engine_settings);

/** 
	* \english
	* Set user unit engine settings.
	* This function sends a structure with a set of engine settings to the controller's memory. These settings specify the motor shaft movement algorithm, list of limitations and rated characteristics. Use it when you change the motor, encoder, positioner etc. Please note that wrong engine settings may lead to device malfunction, which can cause irreversible damage to the board.
	* @see get_engine_settings
	* @param id An identifier of a device
	* @param[in] engine_settings_calb engine settings
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Запись настроек мотора с использованием пользовательских единиц.
	* Настройки определяют номинальные значения напряжения, тока, скорости мотора, характер движения и тип мотора.
	* Пожалуйста, загружайте новые настройки когда вы меняете мотор, энкодер или позиционер.
	* Помните, что неправильные настройки мотора могут повредить оборудование.
	* @see get_engine_settings
	* @param id идентификатор устройства
	* @param[in] engine_settings_calb структура с настройками мотора
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API set_engine_settings_calb (device_t id, const engine_settings_calb_t* engine_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Read engine settings.
	* This function reads the structure containing a set of useful motor settings stored in the controller's memory. These settings specify motor shaft movement algorithm, list of limitations and rated characteristics.
	* @see set_engine_settings
	* @param id An identifier of a device
	* @param[out] engine_settings engine settings
	* \endenglish
	* \russian
	* Чтение настроек мотора.
	* Настройки определяют номинальные значения напряжения, тока, скорости мотора, характер движения и тип мотора.
	* Пожалуйста, загружайте новые настройки когда вы меняете мотор, энкодер или позиционер.
	* Помните, что неправильные настройки мотора могут повредить оборудование.
	* @see set_engine_settings
	* @param id идентификатор устройства
	* @param[out] engine_settings структура с настройками мотора
	* \endrussian
	*/
	result_t XIMC_API get_engine_settings (device_t id, engine_settings_t* engine_settings);

/** 
	* \english
	* Read user unit engine settings.
	* This function reads the structure containing a set of useful motor settings stored in  the controller's memory. These settings specify the motor shaft movement algorithm, list of limitations and rated characteristics.
	* @see set_engine_settings
	* @param id An identifier of a device
	* @param[out] engine_settings_calb engine settings
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Чтение настроек мотора с использованием пользовательских единиц.
	* Настройки определяют номинальные значения напряжения, тока, скорости мотора, характер движения и тип мотора.
	* Пожалуйста, загружайте новые настройки когда вы меняете мотор, энкодер или позиционер.
	* Помните, что неправильные настройки мотора могут повредить оборудование.
	* @see set_engine_settings
	* @param id идентификатор устройства
	* @param[out] engine_settings_calb структура с настройками мотора
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API get_engine_settings_calb (device_t id, engine_settings_calb_t* engine_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Set engine type and driver type.
	* @param id An identifier of a device
	* @param[in] entype_settings structure contains motor type and power driver type settings
	* \endenglish
	* \russian
	* Запись информации о типе мотора и типе силового драйвера.
	* @param id идентификатор устройства
	* @param[in] entype_settings структура, содержащая настройки типа мотора и типа силового драйвера
	* \endrussian
	*/
	result_t XIMC_API set_entype_settings (device_t id, const entype_settings_t* entype_settings);

/** 
	* \english
	* Return engine type and driver type.
	* @param id An identifier of a device
	* @param[out] entype_settings structure contains motor type and power driver type settings
	* \endenglish
	* \russian
	* Возвращает информацию о типе мотора и силового драйвера.
	* @param id идентификатор устройства
	* @param[out] entype_settings структура, содержащая настройки типа мотора и типа силового драйвера
	* \endrussian
	*/
	result_t XIMC_API get_entype_settings (device_t id, entype_settings_t* entype_settings);

/** 
	* \english
	* Set settings of step motor power control.
	* Used with a stepper motor only.
	* @param id An identifier of a device
	* @param[in] power_settings structure contains settings of step motor power control
	* \endenglish
	* \russian
	* Команда записи параметров питания мотора. Используется только с шаговым двигателем.
	* @param id идентификатор устройства
	* @param[in] power_settings структура, содержащая настройки питания шагового мотора
	* \endrussian
	*/
	result_t XIMC_API set_power_settings (device_t id, const power_settings_t* power_settings);

/** 
	* \english
	* Read settings of step motor power control.
	* Used with a stepper motor only.
	* @param id An identifier of a device
	* @param[out] power_settings structure contains settings of step motor power control
	* \endenglish
	* \russian
	* Команда чтения параметров питания мотора. Используется только с шаговым двигателем.
	* Используется только с шаговым двигателем.
	* @param id идентификатор устройства
	* @param[out] power_settings структура, содержащая настройки питания шагового мотора
	* \endrussian
	*/
	result_t XIMC_API get_power_settings (device_t id, power_settings_t* power_settings);

/** 
	* \english
	* Set protection settings.
	* @param id An identifier of a device
	* @param secure_settings structure with secure data
	* \endenglish
	* \russian
	* Команда записи установок защит.
	* @param id идентификатор устройства
	* @param secure_settings структура с настройками критических значений
	* \endrussian
	* @see status_t::flags
	*/
	result_t XIMC_API set_secure_settings (device_t id, const secure_settings_t* secure_settings);

/** 
	* \english
	* Read protection settings.
	* @param id An identifier of a device
	* @param[out] secure_settings critical parameter settings to protect the hardware
	* \endenglish
	* \russian
	* Команда записи установок защит.
	* @param id идентификатор устройства
	* @param[out] secure_settings настройки, определяющие максимально допустимые параметры, для защиты оборудования
	* \endrussian
	* @see status_t::flags
	*/
	result_t XIMC_API get_secure_settings (device_t id, secure_settings_t* secure_settings);

/** 
	* \english
	* Set border and limit switches settings.
	* @see get_edges_settings
	* @param id An identifier of a device
	* @param[in] edges_settings edges settings, specify types of borders, motor behavior and electrical behavior of limit switches
	* \endenglish
	* \russian
	* Запись настроек границ и концевых выключателей.
	* @see get_edges_settings
	* @param id идентификатор устройства
	* @param[in] edges_settings настройки, определяющие тип границ, поведение мотора при их достижении и параметры концевых выключателей
	* \endrussian
	*/
	result_t XIMC_API set_edges_settings (device_t id, const edges_settings_t* edges_settings);

/** 
	* \english
	* Set border and limit switches settings in user units.
	* @see get_edges_settings_calb
	* @param id An identifier of a device
	* @param[in] edges_settings_calb edges settings, specify types of borders, motor behavior and electrical behavior of limit switches
	* @param calibration user unit settings
	* 
	* \note
	* Attention! Some parameters of the edges_settings_calb structure are corrected by the coordinate correction table.
	*
	* \endenglish
	* \russian
	* Запись настроек границ и концевых выключателей с использованием пользовательских единиц.
	* @see get_edges_settings_calb
	* @param id идентификатор устройства
	* @param[in] edges_settings_calb настройки, определяющие тип границ, поведение мотора при их достижении и параметры концевых выключателей
	* @param calibration настройки пользовательских единиц
	*
	* \note
	* Внимание! Некоторые параметры структуры edges_settings_calb корректируются таблицей коррекции координат.  
	*
	* \endrussian
	*/
	result_t XIMC_API set_edges_settings_calb (device_t id, const edges_settings_calb_t* edges_settings_calb, const calibration_t* calibration);

/**  
	* \english
	* Read border and limit switches settings.
	* @see set_edges_settings
	* @param id An identifier of a device
	* @param[out] edges_settings edges settings, types of borders, motor behavior and electrical behavior of limit switches
	* \endenglish
	* \russian
	* Чтение настроек границ и концевых выключателей.
	* @see set_edges_settings
	* @param id идентификатор устройства
	* @param[out] edges_settings настройки, определяющие тип границ, поведение мотора при их достижении и параметры концевых выключателей
	* \endrussian
	*/
	result_t XIMC_API get_edges_settings (device_t id, edges_settings_t* edges_settings);

/**  
	* \english
	* Read border and limit switches settings in user units.
	* @see set_edges_settings_calb
	* @param id An identifier of a device
	* @param[out] edges_settings_calb edges settings, types of borders, motor behavior and electrical behavior of limit switches
	* @param calibration user unit settings
	* 
	* \note
	* Attention! Some parameters of the edges_settings_calb structure are corrected by the coordinate correction table.
	*
	* \endenglish
	* \russian
	* Чтение настроек границ и концевых выключателей с использованием пользовательских единиц.
	* @see set_edges_settings_calb
	* @param id идентификатор устройства
	* @param[out] edges_settings_calb настройки, определяющие тип границ, поведение мотора при их достижении и параметры концевых выключателей
	* @param calibration настройки пользовательских единиц
	*
	* \note
	* Внимание! Некоторые параметры структуры edges_settings_calb корректируются таблицей коррекции координат.  
	*
	* \endrussian
	*/
	result_t XIMC_API get_edges_settings_calb (device_t id, edges_settings_calb_t* edges_settings_calb, const calibration_t* calibration);

/**  
	* \english
	* Set PID settings.
	* This function sends the structure with a set of PID factors to the controller's memory. These settings specify the behavior of the PID routine for the positioner. These factors are slightly different for different positioners. All boards are supplied with the standard set of PID settings in the controller's flash memory. Please use it for loading new PID settings when you change positioner. Please note that wrong PID settings lead to device malfunction.
	* @see get_pid_settings
	* @param id An identifier of a device
	* @param[in] pid_settings PID settings
	* \endenglish
	* \russian
	* Запись ПИД коэффициентов.
	* Эти коэффициенты определяют поведение позиционера.
	* Коэффициенты различны для разных позиционеров.
	* Пожалуйста, загружайте новые настройки, когда вы меняете мотор или позиционер.
	* @see get_pid_settings
	* @param id идентификатор устройства
	* @param[in] pid_settings настройки ПИД
	* \endrussian
	*/
	result_t XIMC_API set_pid_settings (device_t id, const pid_settings_t* pid_settings);

/**  
	* \english
	* Read PID settings.
	* This function reads the structure containing a set of motor PID settings stored in the controller's memory. These settings specify the behavior of the PID routine for the positioner. These factors are slightly different for different positioners. All boards are supplied with the standard set of PID settings in the controller's flash memory.
	* @see set_pid_settings
	* @param id An identifier of a device
	* @param[out] pid_settings PID settings
	* \endenglish
	* \russian
	* Чтение ПИД коэффициентов.
	* Эти коэффициенты определяют поведение позиционера.
	* Коэффициенты различны для разных позиционеров.
	* @see set_pid_settings
	* @param id идентификатор устройства
	* @param[out] pid_settings настройки ПИД
	* \endrussian
	*/
	result_t XIMC_API get_pid_settings (device_t id, pid_settings_t* pid_settings);

/**  
	* \english
	* Set input synchronization settings.
	* This function sends the structure with a set of input synchronization settings that specify the behavior of input synchronization to the controller's memory. All boards are supplied with the standard set of these settings.
	* @see get_sync_in_settings
	* @param id An identifier of a device
	* @param[in] sync_in_settings synchronization settings
	* \endenglish
	* \russian
	* Запись настроек для входного импульса синхронизации.
	* Эта функция записывает структуру с настройками входного импульса синхронизации, определяющими поведение входа синхронизации, в память контроллера.
	* @see get_sync_in_settings
	* @param id идентификатор устройства
	* @param[in] sync_in_settings настройки синхронизации
	* \endrussian
	*/
	result_t XIMC_API set_sync_in_settings (device_t id, const sync_in_settings_t* sync_in_settings);

/**  
	* \english
	* Set input user unit synchronization settings.
	* This function sends the structure with a set of input synchronization settings that specify the behavior of input synchronization to the controller's memory. All boards are supplied with the standard set of these settings.
	* @see get_sync_in_settings_calb
	* @param id An identifier of a device
	* @param[in] sync_in_settings_calb synchronization settings
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Запись настроек для входного импульса синхронизации с использованием пользовательских единиц.
	* Эта функция записывает структуру с настройками входного импульса синхронизации, определяющими поведение входа синхронизации, в память контроллера.
	* @see get_sync_in_settings_calb
	* @param id идентификатор устройства
	* @param[in] sync_in_settings_calb настройки синхронизации
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API set_sync_in_settings_calb (device_t id, const sync_in_settings_calb_t* sync_in_settings_calb, const calibration_t* calibration);

/**  
	* \english
	* Read input synchronization settings.
	* This function reads the structure with a set of input synchronization settings, modes, periods and flags that specify the behavior of input synchronization. All boards are supplied with the standard set of these settings.
	* @see set_sync_in_settings
	* @param id An identifier of a device
	* @param[out] sync_in_settings synchronization settings
	* \endenglish
	* \russian
	* Чтение настроек для входного импульса синхронизации.
	* Эта функция считывает структуру с настройками синхронизации, определяющими поведение входа синхронизации, в память контроллера.
	* @see set_sync_in_settings
	* @param id идентификатор устройства
	* @param[out] sync_in_settings настройки синхронизации
	* \endrussian
	*/
	result_t XIMC_API get_sync_in_settings (device_t id, sync_in_settings_t* sync_in_settings);

/**  
	* \english
	* Read input user unit synchronization settings.
	* This function reads the structure with a set of input synchronization settings, modes, periods and flags that specify the behavior of input synchronization. All boards are supplied with the standard set of these settings.
	* @see set_sync_in_settings_calb
	* @param id An identifier of a device
	* @param[out] sync_in_settings_calb synchronization settings
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Чтение настроек для входного импульса синхронизации с использованием пользовательских единиц.
	* Эта функция считывает структуру с настройками синхронизации, определяющими поведение входа синхронизации, в память контроллера.
	* @see set_sync_in_settings_calb
	* @param id идентификатор устройства
	* @param[out] sync_in_settings_calb настройки синхронизации
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API get_sync_in_settings_calb (device_t id, sync_in_settings_calb_t* sync_in_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Set output synchronization settings.
	* This function sends the structure with a set of output synchronization settings that specify the behavior of output synchronization to the controller's memory. All boards are supplied with the standard set of these settings.
	* @see get_sync_out_settings
	* @param id An identifier of a device
	* @param[in] sync_out_settings synchronization settings
	* \endenglish
	* \russian
	* Запись настроек для выходного импульса синхронизации.
	* Эта функция записывает структуру с настройками выходного импульса синхронизации, определяющими поведение вывода синхронизации, в память контроллера.
	* @see get_sync_in_settings
	* @param id идентификатор устройства
	* @param[in] sync_out_settings настройки синхронизации
	* \endrussian
	*/
	result_t XIMC_API set_sync_out_settings (device_t id, const sync_out_settings_t* sync_out_settings);

/** 
	* \english
	* Set output user unit synchronization settings.
	* This function sends the structure with a set of output synchronization settings that specify the behavior of output synchronization to the controller's memory. All boards are supplied with the standard set of these settings.
	* @see get_sync_in_settings_calb
	* @param id An identifier of a device
	* @param[in] sync_out_settings_calb synchronization settings
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Запись настроек для выходного импульса синхронизации с использованием пользовательских единиц.
	* Эта функция записывает структуру с настройками выходного импульса синхронизации, определяющими поведение вывода синхронизации, в память контроллера.
	* @see get_sync_in_settings_calb
	* @param id идентификатор устройства
	* @param[in] sync_out_settings_calb настройки синхронизации
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API set_sync_out_settings_calb (device_t id, const sync_out_settings_calb_t* sync_out_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Read output synchronization settings.
	* This function reads the structure containing a set of output synchronization settings, modes, periods and flags that specify the behavior of output synchronization.
	* All boards are supplied with the standard set of these settings.
	* @see set_sync_out_settings
	* @param id An identifier of a device
	* @param[out] sync_out_settings synchronization settings
	* \endenglish
	* \russian
	* Чтение настроек для выходного импульса синхронизации.
	* Эта функция считывает структуру с настройками синхронизации, определяющими поведение выхода синхронизации, в память контроллера.
	* \endrussian
	*/
	result_t XIMC_API get_sync_out_settings (device_t id, sync_out_settings_t* sync_out_settings);

/** 
	* \english
	* Read output user unit synchronization settings.
	* This function reads the structure containing a set of output synchronization settings, modes, periods and flags that specify the behavior of output synchronization. All boards are supplied with the standard set of these settings.
	* @see set_sync_in_settings_calb
	* @param id An identifier of a device
	* @param[out] sync_out_settings_calb synchronization settings
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Чтение настроек для выходного импульса синхронизации с использованием пользовательских единиц.
	* Эта функция считывает структуру с настройками синхронизации, определяющими поведение выхода синхронизации, в память контроллера.
	* @see set_sync_in_settings_calb
	* @param id идентификатор устройства
	* @param[in] sync_out_settings_calb настройки синхронизации
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API get_sync_out_settings_calb (device_t id, sync_out_settings_calb_t* sync_out_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Set EXTIO settings.
	* This function sends the structure with a set of EXTIO settings to the controller's memory. By default, input events are signaled through a rising front, and output states are signaled by a high logic state.
	* @see get_extio_settings
	* @param id An identifier of a device
	* @param[in] extio_settings EXTIO settings
	* \endenglish
	* \russian
	* Команда записи параметров настройки режимов внешнего ввода/вывода.
	* Входные события обрабатываются по фронту. Выходные состояния сигнализируются логическим состоянием.
	* По умолчанию нарастающий фронт считается моментом подачи входного сигнала, а единичное состояние считается активным выходом.
	* @see get_extio_settings
	* @param id идентификатор устройства
	* @param[in] extio_settings настройки EXTIO
	* \endrussian
	*/
	result_t XIMC_API set_extio_settings (device_t id, const extio_settings_t* extio_settings);

/** 
	* \english
	* Read EXTIO settings.
	* This function reads a structure with a set of EXTIO settings from the controller's memory.
	* @see set_extio_settings
	* @param id An identifier of a device
	* @param[out] extio_settings EXTIO settings
	* \endenglish
	* \russian
	* Команда чтения параметров настройки режимов внешнего ввода/вывода.
	* @see set_extio_settings
	* @param id идентификатор устройства
	* @param[out] extio_settings настройки EXTIO
	* \endrussian
	*/
	result_t XIMC_API get_extio_settings (device_t id, extio_settings_t* extio_settings);

/** 
	* \english
	* Set brake control settings.
	* @param id An identifier of a device
	* @param[in] brake_settings structure contains brake control settings
	* \endenglish
	* \russian
	* Запись настроек управления тормозом.
	* @param id идентификатор устройства
	* @param[in] brake_settings структура, содержащая настройки управления тормозом
	* \endrussian
	*/
	result_t XIMC_API set_brake_settings (device_t id, const brake_settings_t* brake_settings);

/** 
	* \english
	* Read break control settings.
	* @param id An identifier of a device
	* @param[out] brake_settings structure contains settings of brake control
	* \endenglish
	* \russian
	* Чтение настроек управления тормозом.
	* @param id идентификатор устройства
	* @param[out] brake_settings структура, содержащая настройки управления тормозом
	* \endrussian
	*/
	result_t XIMC_API get_brake_settings (device_t id, brake_settings_t* brake_settings);

/** 
	* \english
	* Read motor control settings.
	*
    * In case of CTL_MODE=1,  joystick motor control is enabled. In this mode, the joystick is maximally displaced, the engine tends to move at MaxSpeed[i]. i=0 if another value hasn't been set at the previous usage. To change the speed index "i", use the buttons.
	*
    * In case of CTL_MODE=2, the motor is controlled by the left/right buttons. When you click on the button, the motor starts moving in the appropriate direction at a speed MaxSpeed[0]. After Timeout[i], motor moves at speed MaxSpeed[i+1]. At the transition between MaxSpeed[i] and MaxSpeed[i+1] the motor just accelerates/decelerates as usual.
	* @param id An identifier of a device
	* @param[in] control_settings structure contains motor control settings.
	* \endenglish
	* \russian
	* Запись настроек управления мотором.
	* При выборе CTL_MODE=1 включается управление мотором с помощью джойстика.
	* В этом режиме при отклонении джойстика на максимум двигатель стремится
	* двигаться со скоростью MaxSpeed [i], где i=0, если предыдущим использованием
	* этого режима не было выбрано другое i. Кнопки переключают номер скорости i.
	* При выборе CTL_MODE=2 включается управление мотором с помощью кнопок
	* left/right. При нажатии на кнопки двигатель начинает двигаться в соответствующую сторону со скоростью MaxSpeed[0], по истечении времени Timeout[i] мотор
	* двигается со скоростью MaxSpeed [i+1]. При
	* переходе от MaxSpeed [i] на MaxSpeed [i+1] действует ускорение, как обычно.
	* @param id идентификатор устройства
	* @param[in] control_settings структура, содержащая настройки управления мотором с помощью джойстика или кнопок влево/вправо.
	* \endrussian
	*/
	result_t XIMC_API set_control_settings (device_t id, const control_settings_t* control_settings);

/** 
	* \english
	* Set motor control settings.
	*
    * In case of CTL_MODE=1, joystick motor control is enabled. In this mode, while the joystick is maximally displaced, the engine tends to move at MaxSpeed[i]. i=0 if another value hasn't been set at the previous usage. To change the speed index "i", use the buttons.
	*
    * In case of CTL_MODE=2, the motor is controlled by the left/right buttons. When you click on the button, the motor starts moving in the appropriate direction at a speed MaxSpeed[0]. After Timeout[i], motor moves at speed MaxSpeed[i+1]. At the transition between MaxSpeed[i] and MaxSpeed[i+1] the motor just accelerates/decelerates as usual.
	* @param id An identifier of a device
	* @param[in] control_settings_calb structure contains motor control settings.
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Запись настроек управления мотором с использованием пользовательских единиц.
	* При выборе CTL_MODE=1 включается управление мотором с помощью джойстика.
	* В этом режиме при отклонении джойстика на максимум двигатель стремится
	* двигаться со скоростью MaxSpeed [i], где i=0, если предыдущим использованием
	* этого режима не было выбрано другое i. Кнопки переключают номер скорости i.
	* При выборе CTL_MODE=2 включается управление мотором с помощью кнопок
	* left/right. При нажатии на кнопки двигатель начинает двигаться в соответствующую сторону со скоростью MaxSpeed [0], по истечении времени Timeout[i] мотор
	* двигается со скоростью MaxSpeed [i+1]. При
	* переходе от MaxSpeed [i] на MaxSpeed [i+1] действует ускорение, как обычно.
	* @param id идентификатор устройства
	* @param[in] control_settings_calb структура, содержащая настройки управления мотором с помощью джойстика или кнопок влево/вправо.
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API set_control_settings_calb (device_t id, const control_settings_calb_t* control_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Read motor control settings.
	*
    * In case of CTL_MODE=1, joystick motor control is enabled. In this mode, while the joystick is maximally displaced, the engine tends to move at MaxSpeed[i]. i=0 if another value hasn't been set at the previous usage. To change the speed index "i", use the buttons.
	*
    * In case of CTL_MODE=2, the motor is controlled by the left/right buttons. When you click on the button, the motor starts moving in the appropriate direction at a speed MaxSpeed[0]. After Timeout[i], motor moves at speed MaxSpeed[i+1]. At the transition between MaxSpeed[i] and MaxSpeed[i+1] the motor just accelerates/decelerates as usual.
	* @param id An identifier of a device
	* @param[out] control_settings structure contains settings motor control by joystick or buttons left/right.
	* \endenglish
	* \russian
	* Чтение настроек управления мотором.
	* При выборе CTL_MODE=1 включается управление мотором с помощью джойстика.
	* В этом режиме при отклонении джойстика на максимум двигатель стремится
	* двигаться со скоростью MaxSpeed [i], где i=0, если предыдущим использованием
	* этого режима не было выбрано другое i. Кнопки переключают номер скорости i.
	* При выборе CTL_MODE=2 включается управление мотором с помощью кнопок
	* left/right. При нажатии на кнопки двигатель начинает двигаться в соответствующую сторону со скоростью MaxSpeed [0], по истечении времени Timeout[i] мотор
	* двигается со скоростью MaxSpeed [i+1]. При
	* переходе от MaxSpeed [i] на MaxSpeed [i+1] действует ускорение, как обычно.
	* @param id идентификатор устройства
	* @param[out] control_settings структура, содержащая настройки управления мотором с помощью джойстика или кнопок влево/вправо.
	* \endrussian
	*/
	result_t XIMC_API get_control_settings (device_t id, control_settings_t* control_settings);

/** 
	* \english
	* Set calibrated motor control settings.
	*
    * In case of CTL_MODE=1, the joystick motor control is enabled. In this mode, while the joystick is maximally displaced, the engine tends to move at MaxSpeed[i]. i=0 if another value hasn't been set at the previous usage. To change the speed index "i", use the buttons.
	*
    * In case of CTL_MODE=2, the motor is controlled by the left/right buttons. When you click on the button, the motor starts moving in the appropriate direction at a speed MaxSpeed[0]. After Timeout[i], motor moves at speed MaxSpeed[i+1]. At the transition between MaxSpeed[i] and MaxSpeed[i+1] the motor just accelerates/decelerates as usual.
	* @param id An identifier of a device
	* @param[out] control_settings_calb structure contains user unit motor control settings.
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Чтение настроек управления мотором с использованием пользовательских единиц.
	* При выборе CTL_MODE=1 включается управление мотором с помощью джойстика.
	* В этом режиме при отклонении джойстика на максимум двигатель стремится
	* двигаться со скоростью MaxSpeed [i], где i=0, если предыдущим использованием
	* этого режима не было выбрано другое i. Кнопки переключают номер скорости i.
	* При выборе CTL_MODE=2 включается управление мотором с помощью кнопок
	* left/right. При нажатии на кнопки двигатель начинает двигаться в соответствующую сторону со скоростью MaxSpeed [0], по истечении времени Timeout[i] мотор
	* двигается со скоростью MaxSpeed [i+1]. При
	* переходе от MaxSpeed [i] на MaxSpeed [i+1] действует ускорение, как обычно.
	* @param id идентификатор устройства
	* @param[out] control_settings_calb структура, содержащая настройки управления мотором с помощью джойстика или кнопок влево/вправо.
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API get_control_settings_calb (device_t id, control_settings_calb_t* control_settings_calb, const calibration_t* calibration);

/** 
	* \english
	* Set joystick position.
	* If joystick position falls outside DeadZone limits, a movement begins. The speed is defined by the joystick's position
	* in the range from the DeadZone limit to the maximum deviation. Joystick positions inside DeadZone limits
        * correspond to zero speed (a "soft stop" command is issued continuously),
	* and positions beyond Low and High limits correspond to MaxSpeed[i] or -MaxSpeed[i] (see command SCTL),
	* where i = 0 by default and can be changed with the left/right buttons (see command SCTL).
	* If the next speed in the list is zero (both integer and microstep parts), the button press is ignored.
        * The first speed in the list shouldn't be zero. DeadZone is defined in 0.1% units.
	* See the Joystick control section on https://doc.xisupport.com/en/8smc5-usb/8SMCn-USB/Technical_specification/Additional_features/Joystick_control.html for more information.
	* @param id An identifier of a device
	* @param[in] joystick_settings structure contains joystick settings
	* \endenglish
	* \russian
	* Запись настроек джойстика.
	* При отклонении джойстика более чем на DeadZone от центрального положения начинается движение со скоростью,
	* определяемой отклонением джойстика от DeadZone до 100% отклонения, причем отклонению DeadZone соответствует
	* нулевая скорость (при этом постоянно выполняется команда "soft stop"), а 100% отклонения соответствует MaxSpeed [i] (см. команду SCTL),
        * где i=0, если предыдущим использованием этого режима не было выбрано другое i.
	* Если следующая скорость в таблице скоростей нулевая (целая и микрошаговая части), то перехода на неё не происходит.
	* Первая скорость в списке не должна быть нулевой. DeadZone вычисляется в десятых долях процента отклонения
	* от центра (JoyCenter) до правого или левого максимума. Подробнее см. раздел "Управление с помощью джойстика" на сайте
	* https://doc.xisupport.com/ru/8smc5-usb/8SMCn-USB/Technical_specification/Additional_features/Joystick_control.html.
	* @param id идентификатор устройства
	* @param[in] joystick_settings структура, содержащая настройки джойстика
	* \endrussian
	*/
	result_t XIMC_API set_joystick_settings (device_t id, const joystick_settings_t* joystick_settings);

/** 
	* \english
	* Read joystick settings.
	* If joystick position falls outside DeadZone limits, a movement begins. The speed is defined by the joystick's position
	* in the range from the DeadZone limit to the maximum deviation. Joystick positions inside DeadZone limits
        * correspond to zero speed (a "soft stop" command is issued continuously),
	* and positions beyond Low and High limits correspond to MaxSpeed[i] or -MaxSpeed[i] (see command SCTL),
	* where i = 0 by default and can be changed with the left/right buttons (see command SCTL).
	* If the next speed in the list is zero (both integer and microstep parts), the button press is ignored.
        * The first speed in the list shouldn't be zero. DeadZone is defined in 0.1% units.
	* See the Joystick control section on https://doc.xisupport.com/en/8smc5-usb/8SMCn-USB/Technical_specification/Additional_features/Joystick_control.html for more information.
	* @param id An identifier of a device
	* @param[out] joystick_settings structure contains joystick settings
	* \endenglish
	* \russian
	* Чтение настроек джойстика.
        * При отклонении джойстика более чем на DeadZone от центрального положения начинается движение со скоростью,
	* определяемой отклонением джойстика от DeadZone до 100% отклонения, причем отклонению DeadZone соответствует
	* нулевая скорость (при этом постоянно выполняется команда "soft stop"), а 100% отклонения соответствует MaxSpeed [i] (см. команду SCTL),
        * где i=0, если предыдущим использованием этого режима не было выбрано другое i.
	* Если следующая скорость в таблице скоростей нулевая (целая и микрошаговая части), то перехода на неё не происходит.
	* Первая скорость в списке не должна быть нулевой. DeadZone вычисляется в десятых долях процента отклонения
	* от центра (JoyCenter) до правого или левого максимума. Подробнее см. раздел "Управление с помощью джойстика" на сайте
	* https://doc.xisupport.com/ru/8smc5-usb/8SMCn-USB/Technical_specification/Additional_features/Joystick_control.html.
	* @param id идентификатор устройства
	* @param[out] joystick_settings структура, содержащая настройки джойстика
	* \endrussian
	*/
	result_t XIMC_API get_joystick_settings (device_t id, joystick_settings_t* joystick_settings);

/** 
	* \english
	* Set control position settings (used with stepper motor only).
	* When controlling the step motor with the encoder (CTP_BASE=0), it is possible to detect the loss of steps. The controller knows the number of steps per revolution (GENG::StepsPerRev) and the encoder resolution (GFBS::IPT). When the control is enabled (CTP_ENABLED is set), the controller stores the current position in the steps of SM and the current position of the encoder. Next, the encoder position is converted into steps at each step, and if the difference between the current position in steps and the encoder position is greater than CTPMinError, the flag STATE_CTP_ERROR is set.
    *
	* Alternatively, the stepper motor may be controlled with the speed sensor (CTP_BASE 1). In this mode, at the active edges of the input clock, the controller stores the current value of steps. Then, at each revolution, the controller checks how many steps have been passed. When the difference is over the CTPMinError, the STATE_CTP_ERROR flag is set.
	* @param id An identifier of a device
	* @param[in] ctp_settings structure contains position control settings.
	* \endenglish
	* \russian
	* Запись настроек контроля позиции(для шагового двигателя).
	* При управлении ШД с энкодером
	* (CTP_BASE 0) появляется возможность обнаруживать потерю шагов. Контроллер знает кол-во шагов на оборот (GENG::StepsPerRev) и разрешение энкодера (GFBS::IPT). При включении контроля (флаг CTP_ENABLED), контроллер запоминает текущую позицию в шагах ШД и текущую позицию энкодера. Далее, на каждом шаге позиция энкодера преобразовывается в шаги и если разница оказывается больше CTPMinError, устанавливается флаг STATE_CTP_ERROR.
    *
	* При управлении ШД с датчиком оборотов (CTP_BASE 1), позиция контролируется по нему. По активному фронту на входе синхронизации контроллер запоминает текущее значение шагов. Далее, при каждом обороте проверяет, на сколько шагов сместились. При рассогласовании более CTPMinError устанавливается флаг STATE_CTP_ERROR.
	* @param id идентификатор устройства
	* @param[in] ctp_settings структура, содержащая настройки контроля позиции
	* \endrussian
	*/
	result_t XIMC_API set_ctp_settings (device_t id, const ctp_settings_t* ctp_settings);

/** 
	* \english
	* Read control position settings (used with stepper motor only).
	* When controlling the step motor with an encoder (CTP_BASE=0), it is possible to detect the loss of steps. The controller knows the number of steps per revolution (GENG::StepsPerRev) and the encoder resolution (GFBS::IPT). When the control is enabled (CTP_ENABLED is set), the controller stores the current position in the steps of SM and the current position of the encoder. Next, the encoder position is converted into steps at each step, and if the difference between the current position in steps and the encoder position is greater than CTPMinError, the flag STATE_CTP_ERROR is set.
    *
    * Alternatively, the stepper motor may be controlled with the speed sensor (CTP_BASE 1). In this mode, at the active edges of the input clock, the controller stores the current value of steps. Then, at each revolution, the controller checks how many steps have been passed. When the difference is over the CTPMinError, the STATE_CTP_ERROR flag is set.
	* @param id An identifier of a device
	* @param[out] ctp_settings structure contains position control settings.
	* \endenglish
	* \russian
	* Чтение настроек контроля позиции(для шагового двигателя).
	* При управлении ШД с энкодером (CTP_BASE 0) появляется возможность обнаруживать потерю шагов. Контроллер знает кол-во шагов на оборот (GENG::StepsPerRev) и разрешение энкодера (GFBS::IPT). При включении контроля (флаг CTP_ENABLED), контроллер запоминает текущую позицию в шагах ШД и текущую позицию энкодера. Далее, на каждом шаге позиция энкодера преобразовывается в шаги и, если разница оказывается больше CTPMinError, устанавливается флаг STATE_CTP_ERROR.
    * При управлении ШД с датчиком оборотов (CTP_BASE 1), позиция контролируется по нему. По активному фронту на входе синхронизации контроллер запоминает текущее значение шагов. Далее, при каждом обороте проверяет, на сколько шагов сместились. При рассогласовании более CTPMinError устанавливается флаг STATE_CTP_ERROR.
	* @param id идентификатор устройства
	* @param[out] ctp_settings структура, содержащая настройки контроля позиции
	* \endrussian
	*/
	result_t XIMC_API get_ctp_settings (device_t id, ctp_settings_t* ctp_settings);

/** 
	* \english
	* Set UART settings.
	* This function sends the structure with UART settings to the controller's memory.
	* @see uart_settings_t
	* @param Speed UART speed
	* @param[in] uart_settings UART settings
	* \endenglish
	* \russian
	*  Команда записи настроек UART.
	* Эта функция записывает структуру настроек UART в память контроллера.
	* @see uart_settings_t
	* @param Speed Cкорость UART
	* @param[in] uart_settings настройки UART
	* \endrussian
	*/
	result_t XIMC_API set_uart_settings (device_t id, const uart_settings_t* uart_settings);

/** 
	* \english
	* Read UART settings.
	* This function reads the structure containing UART settings.
	* @see uart_settings_t
	* @param Speed UART speed
	* @param[out] uart_settings UART settings
	* \endenglish
	* \russian
	* Команда чтения настроек UART.
	* Эта функция заполняет структуру настроек UART.
	* @see uart_settings_t
	* @param Speed Cкорость UART
	* @param[out] uart_settings настройки UART
	* \endrussian
	*/
	result_t XIMC_API get_uart_settings (device_t id, uart_settings_t* uart_settings);

/** 
	* \english
	* Set network settings. Manufacturer only.
	* This function sets the desired network settings.
	* @see net_settings_t
	* @param DHCPEnabled DHCP enabled (1) or not (0)
	* @param IPv4Address[4] Array[4] with an IP address
	* @param SubnetMask[4] Array[4] with a subnet mask address
	* @param DefaultGateway[4] Array[4] with a default gateway address
	* \endenglish
	* \russian
	* Команда записи сететвых настроек. Только для производителя.
	* Эта функция меняет сетевые настройки на заданные.
	* @param DHCPEnabled DHCP включен (1) или нет (0)
	* @param IPv4Address[4] Массив[4] с IP-адресом
	* @param SubnetMask[4] Массив[4] с маской подсети
	* @param DefaultGateway[4] Массив[4] со шлюзом сети
	* \endrussian
	*/
	result_t XIMC_API set_network_settings (device_t id, const network_settings_t* network_settings);

/** 
	* \english
	* Read network settings. Manufacturer only.
	* This function returns the current network settings.
	* @see net_settings_t
	* @param DHCPEnabled DHCP enabled (1) or not (0)
	* @param IPv4Address[4] Array[4] with an IP address
	* @param SubnetMask[4] Array[4] with a subnet mask address
	* @param DefaultGateway[4] Array[4] with a default gateway address
	* \endenglish
	* \russian
	* Команда чтения сететвых настроек. Только для производителя.
	* Эта функция возвращает текущие сетевые настройки.
	* @param DHCPEnabled DHCP включен (1) или нет (0)
	* @param IPv4Address[4] Массив[4] с IP-адресом
	* @param SubnetMask[4] Массив[4] с маской подсети
	* @param DefaultGateway[4] Массив[4] со шлюзом сети
	* \endrussian
	*/
	result_t XIMC_API get_network_settings (device_t id, network_settings_t* network_settings);

/** 
	* \english
	* Sets the password. Manufacturer only.
	* This function sets the user password for the device's web-page.
	* @see pwd_settings_t
	* @param UserPassword[20] Password for web-page
	* \endenglish
	* \russian
	* Команда записи пароля к веб-странице. Только для производителя.
	* Эта функция меняет пользовательский пароль к веб-странице.
	* @param UserPassword[20] Строчка-пароль для доступа к веб-странице
	* \endrussian
	*/
	result_t XIMC_API set_password_settings (device_t id, const password_settings_t* password_settings);

/** 
	* \english
	* Read the password. Manufacturer only.
	* This function reads the user password for the device's web-page.
	* @see pwd_settings_t
	* @param UserPassword[20] Password for web-page
	* \endenglish
	* \russian
	* Команда чтения пароля к веб-странице. Только для производителя.
	* Эта функция пользователяет прочитать пользовательский пароль к веб-странице из памяти контроллера.
	* @param UserPassword[20] Строчка-пароль для доступа к веб-странице
	* \endrussian
	*/
	result_t XIMC_API get_password_settings (device_t id, password_settings_t* password_settings);

/** 
	* \english
	* Set calibration settings. Manufacturer only.
	* This function sends the structure with calibration settings to the controller's memory. These settings are used to convert bare ADC values to winding currents in mA and the full current in mA. Parameters are grouped into pairs, XXX_A and XXX_B, representing linear equation coefficients. The first one is the slope, the second one is the constant term. Thus, XXX_Current[mA] = XXX_A[mA/ADC]*XXX_ADC_CODE[ADC] + XXX_B[mA].
	* @see calibration_settings_t
	* @param id An identifier of a device
	* @param[in] calibration_settings calibration settings
	* \endenglish
	* \russian
	* Команда записи калибровочных коэффициентов. Команда только для производителя.
	* Эта функция записывает структуру калибровочных коэффициентов в память контроллера. Эти коэффициенты используются для пересчёта кодов АЦП в токи обмоток и полный ток потребления. Коэффициенты сгруппированы в пары, XXX_A и XXX_B; пары представляют собой коэффициенты линейного уравнения. Первый коэффициент - тангенс угла наклона, второй - постоянное смещение. Таким образом, XXX_Current[mA] = XXX_A[mA/ADC]*XXX_ADC_CODE[ADC] + XXX_B[mA].
	* @see calibration_settings_t
	* @param id идентификатор устройства
	* @param[in] calibration_settings калибровочные коэффициенты
	* \endrussian
	*/
	result_t XIMC_API set_calibration_settings (device_t id, const calibration_settings_t* calibration_settings);

/** 
	* \english
	* Read calibration settings. Manufacturer only. 
	* This function reads the structure with calibration settings. These settings are used to convert bare ADC values to winding currents in mA and the full current in mA. Parameters are grouped into pairs, XXX_A and XXX_B, representing linear equation coefficients. The first one is the slope, the second one is the constant term. Thus, XXX_Current[mA] = XXX_A[mA/ADC]*XXX_ADC_CODE[ADC] + XXX_B[mA].
	* @see calibration_settings_t
	* @param id An identifier of a device
	* @param[out] calibration_settings calibration settings
	* \endenglish
	* \russian
	* Команда чтения калибровочных коэффициентов. Команда только для производителя.
	* Эта функция заполняет структуру калибровочных коэффициентов. Эти коэффициенты используются для пересчёта кодов АЦП в токи обмоток и полный ток потребления. Коэффициенты сгруппированы в пары, XXX_A и XXX_B; пары представляют собой коэффициенты линейного уравнения. Первый коэффициент - тангенс угла наклона, второй - постоянное смещение. Таким образом, XXX_Current[mA] = XXX_A[mA/ADC]*XXX_ADC_CODE[ADC] + XXX_B[mA].
	* @see calibration_settings_t
	* @param id идентификатор устройства
	* @param[out] calibration_settings калибровочные коэффициенты
	* \endrussian
	*/
	result_t XIMC_API get_calibration_settings (device_t id, calibration_settings_t* calibration_settings);

/** 
	* \english
	* Write user's controller name and internal settings to the FRAM.
	* @param id An identifier of a device
	* @param[in] controller_name structure contains the previously set user's controller name
	* \endenglish
	* \russian
	* Запись пользовательского имени контроллера и настроек в FRAM.
	* @param id идентификатор устройства
	* @param[in] controller_information структура, содержащая информацию о контроллере
	* \endrussian
	*/
	result_t XIMC_API set_controller_name (device_t id, const controller_name_t* controller_name);

/** 
	* \english
	* Read user's controller name and internal settings from the FRAM.
	* @param id An identifier of a device
	* @param[out] controller_name structure contains previously set user controller name
	* \endenglish
	* \russian
	* Чтение пользовательского имени контроллера и настроек из FRAM.
	* @param id идентификатор устройства
	* @param[out] controller_name структура, содержащая установленное пользовательское имя контроллера и флаги настроек
	* \endrussian
	*/
	result_t XIMC_API get_controller_name (device_t id, controller_name_t* controller_name);

/** 
	* \english
	* Write user data into the FRAM.
	* @param id An identifier of a device
	* @param[in] nonvolatile_memory user data.
	* \endenglish
	* \russian
	* Запись пользовательских данных во FRAM.
	* @param id идентификатор устройства
	* @param[in] nonvolatile_memory структура, содержащая установленные пользовательские данные
	* \endrussian
	*/
	result_t XIMC_API set_nonvolatile_memory (device_t id, const nonvolatile_memory_t* nonvolatile_memory);

/** 
	* \english
	* Read user data from FRAM.
	* @param id An identifier of a device
	* @param[out] nonvolatile_memory structure contains previously set user data.
	* \endenglish
	* \russian
	* Чтение пользовательских данных из FRAM.
	* @param id идентификатор устройства
	* @param[out] nonvolatile_memory структура, содержащая установленные пользовательские данные
	* \endrussian
	*/
	result_t XIMC_API get_nonvolatile_memory (device_t id, nonvolatile_memory_t* nonvolatile_memory);

/**  
	* \english
	* Set electromechanical coefficients.
	* The settings are different for different stepper motors. Please set new settings when you change the motor.
	* @see get_emf_settings
	* @param id An identifier of a device
	* @param[in] emf_settings EMF settings
	* \endenglish
	* \russian
	* Запись электромеханических настроек шагового двигателя. 
	* Настройки различны для разных двигателей.
	* Пожалуйста, загружайте новые настройки, когда вы меняете мотор.
	* @see get_emf_settings
	* @param id идентификатор устройства
	* @param[in] emf_settings настройки EMF
	* \endrussian
	*/
	result_t XIMC_API set_emf_settings (device_t id, const emf_settings_t* emf_settings);

/**  
	* \english
	* Read electromechanical settings.
	* The settings are different for different stepper motors.
	* @see set_emf_settings
	* @param id An identifier of a device
	* @param[out] emf_settings EMF settings
	* \endenglish
	* \russian
	* Чтение электромеханических настроек шагового двигателя. 
	* Настройки различны для разных двигателей.
	* @see set_emf_settings
	* @param id идентификатор устройства
	* @param[out] emf_settings настройки EMF
	* \endrussian
	*/
	result_t XIMC_API get_emf_settings (device_t id, emf_settings_t* emf_settings);

/**  
	* \english
	* Set engine advanced settings.
	* @see get_engine_advansed_setup
	* @param id An identifier of a device
	* @param[in] engine_advansed_setup EAS settings
	* \endenglish
	* \russian
	* Запись расширенных настроек. 
	* @see get_engine_advansed_setup
	* @param id идентификатор устройства
	* @param[in] engine_advansed_setup настройки EAS
	* \endrussian
	*/
	result_t XIMC_API set_engine_advansed_setup (device_t id, const engine_advansed_setup_t* engine_advansed_setup);

/**  
	* \english
	* Read engine advanced settings.
	* @see set_engine_advansed_setup
	* @param id An identifier of a device
	* @param[out] engine_advansed_setup EAS settings
	* \endenglish
	* \russian
	* Чтение расширенных настроек.
	* @see set_engine_advansed_setup
	* @param id идентификатор устройства
	* @param[out] engine_advansed_setup настройки EAS
	* \endrussian
	*/
	result_t XIMC_API get_engine_advansed_setup (device_t id, engine_advansed_setup_t* engine_advansed_setup);

/**  
	* \english
	* Set extended settings. Currently, it is not in use.
	* @see get_extended_settings
	* @param id An identifier of a device
	* @param[in] extended_settings EST settings
	* \endenglish
	* \russian
	* Запись расширенных настроек. В настоящее время не используется.
	* @see get_extended_settings
	* @param id идентификатор устройства
	* @param[in] extended_settings настройки EST
	* \endrussian
	*/
	result_t XIMC_API set_extended_settings (device_t id, const extended_settings_t* extended_settings);

/**  
	* \english
	* Read extended settings. Currently, it is not in use.
	* @see set_extended_settings 
	* @param id An identifier of a device
	* @param[out] extended_settings EST settings
	* \endenglish
	* \russian
	* Чтение расширенных настроек. В настоящее время не используется.
	* @see set_extended_settings
	* @param id идентификатор устройства
	* @param[out] extended_settings настройки EST
	* \endrussian
	*/
	result_t XIMC_API get_extended_settings (device_t id, extended_settings_t* extended_settings);


//@}

/**
	* \english
	* @name Group of commands movement control
	*
	* \endenglish
	* \russian
	* @name Группа команд управления движением
	*
	* \endrussian
	*/

//@{

/**  
	* \english
	* Immediately stops the engine, moves it to the STOP state, and sets switches to BREAK mode (windings are short-circuited). The holding regime is deactivated for DC motors, keeping current in the windings for stepper motors (to control it, see Power management settings).
	*
    * When this command is called, the ALARM flag is reset.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Немедленная остановка двигателя, переход в состояние STOP,
	* ключи в режиме BREAK (обмотки накоротко замкнуты), режим
	* "удержания" дезактивируется для DC двигателей, удержание тока
	* в обмотках для шаговых двигателей (с учётом Power management
	* настроек).
	* При вызове этой команды сбрасывается флаг ALARM.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_stop (device_t id);

/** 
	* \english
	* Immediately power off the motor regardless its state.
	*
	* Shouldn't be used during motion as the motor could be powered on again automatically to continue movement. The command is designed to manually power off the motor. When automatic power off after stop is required, use the power management system.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Немедленное отключение питания двигателя вне зависимости от его состояния. Команда предназначена для ручного управления питанием двигателя. Не следует использовать эту команду для отключения двигателя во время движения, так как питание может снова включиться для завершения движения. Для автоматического управления питанием двигателя и его отключения после остановки следует использовать систему управления электропитанием.
	* @param id идентификатор устройства
	* \endrussian
	* @see get_power_settings
	* @see set_power_settings
	*/
	result_t XIMC_API command_power_off (device_t id);

/** 
	* \english
	* Move to position.
	* Upon receiving the command "move" the engine starts to move with pre-set parameters (speed, acceleration, retention), to the point specified by  Position and uPosition. uPosition sets the microstep position of a stepper motor. In the case of DC motor, this field is ignored.
	* @param id An identifier of a device
	* @param Position position to move.
	* @param uPosition the fractional part of the position to move, in microsteps. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings).
	* \endenglish
	* \russian
	* При получении команды "move" двигатель начинает перемещаться (если не используется
	* режим "ТТЛСинхроВхода"), с заранее установленными параметрами (скорость, ускорение,
	* удержание), к точке указанной в полях Position, uPosition. Для шагового мотора
	* uPosition задает значение микрошага, для DC мотора это поле не используется.
	* @param id идентификатор устройства
	* @param Position заданная позиция.
	* @param uPosition часть позиции в микрошагах. Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings).	
	* \endrussian
	*/
	result_t XIMC_API command_move (device_t id, int Position, int uPosition);

/** 
	* \english
	* Move to position using user units.
	* Upon receiving the command "move" the engine starts to move with preset parameters (speed, acceleration, retention), to the point specified by Position.
	* @param id An identifier of a device
	* @param Position position to move.
	* @param calibration user unit settings
	* 
	* \note
	* The parameter Position is adjusted by the correction table.
	*
	* \endenglish
	* \russian
	* Перемещение в позицию  с использованием пользовательских единиц.
	* При получении команды "move" двигатель начинает перемещаться (если не используется
	* режим "ТТЛСинхроВхода"), с заранее установленными параметрами (скорость, ускорение,
	* удержание), к точке указанной в поле Position. 
	* @param id идентификатор устройства
	* @param Position позиция для перемещения
	* @param calibration настройки пользовательских единиц
	*
	* \note
	* Параметр Position корректируется таблицей коррекции.
	*
	* \endrussian
	*/
	result_t XIMC_API command_move_calb (device_t id, float Position, const calibration_t* calibration);

/** 
	* \english
	* Shift by a set offset.
	* Upon receiving the command "movr", the engine starts to move with preset parameters (speed, acceleration, hold) left or right (depending on the sign of DeltaPosition). It moves by the number of steps specified in the fields DeltaPosition and uDeltaPosition. uDeltaPosition sets the microstep offset for a stepper motor. In the case of a DC motor, this field is ignored.
	* @param DeltaPosition shift from initial position.
	* @param uDeltaPosition the fractional part of the offset shift, in microsteps. The microstep size and the range of valid values for this field depend on the selected step division mode (see the MicrostepMode field in engine_settings).
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Перемещение на заданное смещение.
	* При получении команды "movr" двигатель начинает смещаться (если не используется режим "ТТЛСинхроВхода"), с заранее установленными параметрами (скорость, ускорение, удержание), влево или вправо (зависит от знака DeltaPosition) на количество импульсов указанное в полях DeltaPosition, uDeltaPosition. Для шагового мотора uDeltaPosition задает значение микрошага, для DC мотора это поле не используется.
	* @param DeltaPosition смещение.
	* @param uDeltaPosition часть смещения в микрошагах. Величина микрошага и диапазон допустимых значений для данного поля зависят от выбранного режима деления шага (см. поле MicrostepMode в engine_settings).
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_movr (device_t id, int DeltaPosition, int uDeltaPosition);

/** 
	* \english
	* Shift by a set offset using user units.
	* Upon receiving the command "movr", the engine starts to move with preset parameters (speed, acceleration, hold) left or right (depending on the sign of DeltaPosition). It moves by the distance specified in the field DeltaPosition.
	* @param DeltaPosition shift from initial position.
	* @param id An identifier of a device
	* @param user unit calibration settings 
	*
	* \note
	* The final coordinate is calculated using DeltaPosition and adjusted by the correction table. However, the correction cannot be done if the motor moves. movr sets the target position equal to the current target position, shifted by delta. But the library can't determine the current target position while moving. So there is no possibility of calculating the final position and correcting it with the correction table.
	*
	* \endenglish
	* \russian
	* Перемещение на заданное смещение с использованием пользовательских единиц.
	* При получении команды "movr" двигатель начинает смещаться (если не используется режим "ТТЛСинхроВхода"), с заранее установленными параметрами (скорость, ускорение, удержание), влево или вправо (зависит от знака DeltaPosition) на расстояние указанное в поле DeltaPosition.
	* @param DeltaPosition смещение.
	* @param id идентификатор устройства
	* @param calibration настройки пользовательских единиц
	*
	* \note
	* Конечная координата вычисляемая с помощью DeltaPosition, корректируется таблицей коррекции. Однако корректировка не может быть применена в случае поступления команды movr во время движения. Команда movr устанавливает целевую позицию равной текущей целевой плюс дельта. Но точно определить текущую целевую координату во время движения библиотека не может. Поэтому она не может рассчитать конечную позицию и соответсвующую ей коррекцию.
	*
	* \endrussian
	*/
	result_t XIMC_API command_movr_calb (device_t id, float DeltaPosition, const calibration_t* calibration);

/** 
	* \english
	* Moving to home position.
	*
    * Moving algorithm:
	*
    * 1) Moves the motor according to the speed FastHome, uFastHome and flag HOME_DIR_FAST until the limit switch if the HOME_STOP_ENDS flag is set. Or moves the motor until the input synchronization signal occurs if the flag HOME_STOP_SYNC is set. Or moves until the revolution sensor signal occurs if the flag HOME_STOP_REV_SN is set.
	*
    * 2) Then moves according to the speed SlowHome, uSlowHome and flag HOME_DIR_SLOW until the input clock signal occurs if the flag HOME_MV_SEC is set. If the flag HOME_MV_SEC is reset, skip this step.
	*
    * 3) Then shifts the motor according to the speed FastHome, uFastHome and the flag HOME_DIR_SLOW by HomeDelta distance, uHomeDelta.
    * 
    * See GHOM/SHOM commands' description for details on home flags.
	*
    * Moving settings can be set by set_home_settings/set_home_settings_calb.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Движение в домашнюю позицию.
	*
	* Алгоритм движения:
	*
	* 1) Двигает мотор согласно скоростям FastHome, uFastHome и флагу HOME_DIR_FAST до достижения концевого выключателя, если флаг HOME_STOP_ENDS установлен. Или двигает  до достижения сигнала с входа синхронизации, если установлен флаг HOME_STOP_SYNC. Или до поступления сигнала с датчика оборотов, если установлен флаг HOME_STOP_REV_SN
	*
	* 2) далее двигает согласно скоростям SlowHome, uSlowHome и флагу HOME_DIR_SLOW до достижения сигнала с входа синхронизации, если установлен флаг HOME_MV_SEC. Если флаг HOME_MV_SEC сброшен, пропускаем этот пункт.
	*
	* 3) далее двигает мотор согласно скоростям FastHome, uFastHome и флагу HOME_DIR_SLOW на расстояние HomeDelta, uHomeDelta.
	*
	* Описание флагов и переменных см. описание команд GHOM/SHOM
	* @param id идентификатор устройства
	* \endrussian
	@see home_settings_t
	@see get_home_settings
	@see set_home_settings
	*/
	result_t XIMC_API command_home (device_t id);

/** 
	* \english
	* Start continuous moving to the left.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* При получении команды "left" двигатель начинает смещаться, с заранее установленными параметрами (скорость, ускорение), влево.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_left (device_t id);

/** 
	* \english
	* Start continuous moving to the right.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* При получении команды "rigt" двигатель начинает смещаться, с заранее установленными параметрами (скорость, ускорение), вправо.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_right (device_t id);

/** 
	* \english
	* Upon receiving the command "loft", the engine is shifted from the current position to a distance Antiplay defined in engine settings. Then moves to the initial position.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* При получении команды "loft" двигатель смещается из текущей точки на
	* расстояние Antiplay, заданное в настройках мотора (engine_settings), затем двигается в ту же точку.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_loft (device_t id);

/** 
	* \english
	* Soft stop the engine. The motor is slowing down with the deceleration specified in move_settings.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Плавная остановка. Двигатель останавливается с ускорением замедления.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_sstp (device_t id);

/** 
	* \english
	* Reads the value position in steps and microsteps for stepper motor and encoder steps for all engines.
	* @param id An identifier of a device
	* @param[out] the_get_position structure contains motor position.
	* \endenglish
	* \russian
	* Считывает значение положения в шагах и микрошагах для шагового двигателя и в шагах энкодера
	* всех двигателей.
	* @param id идентификатор устройства
	* @param[out] the_get_position структура, содержащая позицию мотора.
	* \endrussian
	*/
	result_t XIMC_API get_position (device_t id, get_position_t* the_get_position);

/** 
	* \english
	* Reads position value in user units for stepper motor and encoder steps for all engines.
	* @param id An identifier of a device
	* @param[out] the_get_position_calb structure contains motor position.
	* @param calibration user unit settings
	* 
	* \note
	* Attention! Some parameters of the get_position_calb structure are corrected by the coordinate correction table.
	*
	* \endenglish
	* \russian
	* Считывает значение положения в пользовательских единицах для шагового двигателя и в шагах энкодера всех двигателей.
	* @param id идентификатор устройства
	* @param[out] the_get_position_calb структура, содержащая позицию мотора.
	* @param calibration настройки пользовательских единиц
	*
	* \note
	* Внимание! Некоторые параметры структуры get_position_calb корректируются таблицей коррекции координат.  
	*
	* \endrussian
	*/
	result_t XIMC_API get_position_calb (device_t id, get_position_calb_t* the_get_position_calb, const calibration_t* calibration);

/** 
	* \english
	* Sets position in steps and microsteps for stepper motor. Sets encoder position for all engines.
	* @param id An identifier of a device
	* @param[out] the_set_position structure contains motor position.
	* \endenglish
	* \russian
	* Устанавливает произвольное значение положения в шагах и
	* микрошагах для шагового двигателя и в шагах энкодера для всех
	* двигателей.
	* @param id идентификатор устройства
	* @param[out] the_set_position структура, содержащая позицию мотора.
	* \endrussian
	*/
	result_t XIMC_API set_position (device_t id, const set_position_t* the_set_position);

/** 
	* \english
	* Sets any position value and encoder value of all engines. In user units.
	* @param id An identifier of a device
	* @param[out] the_set_position_calb structure contains motor position.
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Устанавливает произвольное значение положения и значение энкодера всех двигателей с использованием пользовательских единиц.
	* @param id идентификатор устройства
	* @param[out] the_set_position_calb структура, содержащая позицию мотора.
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
	result_t XIMC_API set_position_calb (device_t id, const set_position_calb_t* the_set_position_calb, const calibration_t* calibration);

/** 
	* \english
	* Sets the current position to 0. Sets the target position of the move command and the movr command to zero for all cases except for movement to the target position. In the latter case, the target position is calculated so that the absolute position of the destination stays the same. For example, if we were at 400 and moved to 500, then the command Zero makes the current position 0 and the position of the destination 100. It does not change the mode of movement. If the motion is carried, it continues, and if the engine is in the "hold", the type of retention remains.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Устанавливает текущую позицию равной 0. Устанавливает позицию, в которую осуществляется движение по командам move и movr, равной нулю во всех случаях, кроме движения к позиции назначения. В последнем случае позиция назначения пересчитывается так, что в абсолютном положении точка назначения не меняется. То есть если мы находились в точке 400 и двигались к 500, то команда Zero делает текущую позицию 0, а позицию назначения - 100. Не изменяет режим движения: т.е. если движение осуществлялось, то оно продолжается; если мотор находился в режиме "удержания", то тип удержания сохраняется.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_zero (device_t id);


//@}

/**
	* \english
	* @name Group of save settings and load settings commands
	*
	* \endenglish
	* \russian
	* @name Группа команд сохранения и загрузки настроек
	*
	* \endrussian
	*/

//@{

/** 
	* \english
	* Save all settings from the controller's RAM to the controller's flash memory, replacing previous data in the flash memory.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* При получении команды контроллер выполняет операцию сохранения текущих настроек во встроенную энергонезависимую память контроллера.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_save_settings (device_t id);

/** 
	* \english
	* Read all settings from the controller's flash memory to the controller's RAM, replacing previous data in the RAM.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Чтение всех настроек контроллера из flash памяти в оперативную, заменяя текущие настройки.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_read_settings (device_t id);

/** 
	* \english
	* Save important settings (calibration coefficients, etc.) from the controller's RAM to the controller's flash memory, replacing previous data in the flash memory. Manufacturer only.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* При получении команды контроллер выполняет операцию сохранения важных настроек (калибровочные коэффициенты и т.п.) во встроенную энергонезависимую память контроллера. Только для производителя.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_save_robust_settings (device_t id);

/** 
	* \english
	* Read important settings (calibration coefficients, etc.) from the controller's flash memory to the controller's RAM, replacing previous data in the RAM. Manufacturer only.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Чтение важных настроек (калибровочные коэффициенты и т.п.) контроллера из flash памяти в оперативную, заменяя текущие настройки. Только для производителя.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_read_robust_settings (device_t id);

/** 
	* \english
	* Save settings from the controller's RAM to the stage's EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Запись настроек контроллера в EEPROM память позиционера
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_eesave_settings (device_t id);

/** 
	* \english
	* Read settings from the stage's EEPROM to the controller's RAM. This operation is performed automatically at the connection of the stage with an EEPROM to the controller. Can be used by the manufacturer only.
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Чтение настроек контроллера из EEPROM памяти позиционера. Эта операция также автоматически выполняется при подключении позиционера с EEPROM памятью. Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_eeread_settings (device_t id);

/** 
	* \english
	* Start measurements and buffering of speed and the speed error (target speed minus real speed).
	* @param id An identifier of a device
	* \endenglish
	* \russian
	* Начать измерения и буферизацию скорости, ошибки следования.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_start_measurements (device_t id);

/** 
	* \english
	* A command to read the data buffer to build a speed graph and a speed error graph.
	* 
	* Filling the buffer starts with the command "start_measurements". The buffer holds 25 points; the points are taken with a period of 1 ms. To create a robust system, read data every 20 ms. If the buffer is full, it is recommended to repeat the readings every 5 ms until the buffer again becomes filled with 20 points.
	*
	* To stop measurements just stop reading data. After buffer overflow measurements will stop automatically.
	* @see measurements_t
	* @param id An identifier of a device
	* @param[out] measurements structure with buffer and its length.
	* \endenglish
	* \russian
	* Команда чтения буфера данных для построения графиков скорости и ошибки следования. Заполнение буфера начинается по команде "start_measurements". Буфер вмещает 25 точек, точки снимаются с периодом 1 мс. Для создания устойчивой системы следует считывать данные каждые 20 мс, если буфер полностью заполнен, то рекомендуется повторять считывания каждые 5 мс до момента пока буфер вновь не станет заполнен 20-ю точками.
	* @see measurements_t
	* @param id идентификатор устройства
	* @param[out] measurements структура с буфером и его длинной.
	* \endrussian
	*/
	result_t XIMC_API get_measurements (device_t id, measurements_t* measurements);

/** 
	* \english
	* Return device electrical parameters, useful for charts.
	*
	* A useful function that fills the structure with a snapshot of the controller voltages and currents.
	* @see chart_data_t
	* @param id An identifier of a device
	* @param[out] chart_data structure with a snapshot of controller parameters.
	* \endenglish
	* \russian
	* Команда чтения состояния обмоток и других не часто используемых данных. Предназначена в первую очередь для получения данных для построения графиков в паре с командой GETS.
	* @see chart_data_t
	* @param id идентификатор устройства
	* @param[out] chart_data структура chart_data.
	* \endrussian
	*/
	result_t XIMC_API get_chart_data (device_t id, chart_data_t* chart_data);

/** 
	* \english
	* Read device serial number.
	* @param id An identifier of a device
	* @param[out] SerialNumber serial number
	* \endenglish
	* \russian
	* Чтение серийного номера контроллера.
	* @param id идентификатор устройства
	* @param[out] SerialNumber серийный номер контроллера
	* \endrussian
	*/
	result_t XIMC_API get_serial_number (device_t id, unsigned int* SerialNumber);

/** 
	* \english
	* Read the controller's firmware version.
	* @param id An identifier of a device
	* @param[out] Major major version
	* @param[out] Minor minor version
	* @param[out] Release release version
	* \endenglish
	* \russian
	* Чтение номера версии прошивки контроллера.
	* @param id идентификатор устройства
	* @param[out] Major номер основной версии
	* @param[out] Minor номер дополнительной версии
	* @param[out] Release номер релиза
	* \endrussian
	*/
	result_t XIMC_API get_firmware_version (device_t id, unsigned int* Major, unsigned int* Minor, unsigned int* Release);

/** 
	* \english
	* The command switches the controller to update the firmware state. Manufacturer only.
	* After receiving this command, the firmware board sets a flag (for loader), sends an echo reply, and restarts the controller.
	* \endenglish
	* \russian
	* Команда переводит контроллер в режим обновления прошивки. Только для производителя.
	* Получив такую команду, прошивка платы устанавливает флаг (для загрузчика), отправляет эхо-ответ и перезагружает контроллер.
	* \endrussian
	*/
	result_t XIMC_API service_command_updf (device_t id);


//@}

/**
	* \english
	* @name Service commands
	*
	* \endenglish
	* \russian
	* @name Группа сервисных команд
	*
	* \endrussian
	*/

//@{

/** 
	* \english
	* Write device serial number and hardware version to the controller's flash memory.
	* Along with the new serial number and hardware version, a "Key" is transmitted.
	* The SN and hardware version are changed and saved when keys match.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] serial_number structure contains new serial number and secret key.
	* \endenglish
	* \russian
	* Запись серийного номера и версии железа во flash память контроллера.
	* Вместе с новым серийным номером и версией железа передаётся "Ключ",
	* только при совпадении которого происходит изменение и сохранение.
	* Функция используется только производителем.
	* @param id идентификатор устройства
	* @param[in] serial_number структура, содержащая серийный номер, версию железа и ключ.
	* \endrussian
	*/
	result_t XIMC_API set_serial_number (device_t id, const serial_number_t* serial_number);

/** 
	* \english
	* Read the analog data structure that contains raw analog data from the embedded ADC. This function is used for device testing and deep recalibration by the manufacturer only.
	* @param id An identifier of a device
	* @param[out] analog_data analog data coefficients
	* \endenglish
	* \russian
	* Чтение аналоговых данных, содержащих данные с АЦП и нормированные значения величин. Эта функция используется для тестирования и калибровки устройства.
	* @param id идентификатор устройства
	* @param[out] analog_data аналоговые данные
	* \endrussian
	*/
	result_t XIMC_API get_analog_data (device_t id, analog_data_t* analog_data);

/** 
	* \english
	* Read data from firmware for debug purpose. Manufacturer only.
	* Its use depends on context, firmware version and previous history.
	* @param id An identifier of a device
	* @param[out] debug_read Debug data.
	* \endenglish
	* \russian
	* Чтение данных из прошивки для отладки и поиска неисправностей. Команда только для производителя.
	* Получаемые данные зависят от версии прошивки, истории и контекста использования.
	* @param id идентификатор устройства
	* @param[out] debug_read Данные для отладки.
	* \endrussian
	*/
	result_t XIMC_API get_debug_read (device_t id, debug_read_t* debug_read);

/** 
	* \english
	* Write data to firmware for debug purpose. Manufacturer only.
	* @param id An identifier of a device
	* @param[in] debug_write Debug data.
	* \endenglish
	* \russian
	* Запись данных в прошивку для отладки и поиска неисправностей. Команда только для производителя.
	* @param id идентификатор устройства
	* @param[in] debug_write Данные для отладки.
	* \endrussian
	*/
	result_t XIMC_API set_debug_write (device_t id, const debug_write_t* debug_write);


//@}

/**
	* \english
	* @name A group of EEPROM commands
	*
	* \endenglish
	* \russian
	* @name Группа команд работы с EEPROM подвижки
	*
	* \endrussian
	*/

//@{

/** 
	* \english
	* Write the user's stage name to EEPROM.
	* @param id An identifier of a device
	* @param[in] stage_name structure contains the previously set user's stage name.
	* \endenglish
	* \russian
	* Запись пользовательского имени подвижки в EEPROM.
	* @param id идентификатор устройства
	* @param[in] stage_name структура, содержащая установленное пользовательское имя позиционера
	* \endrussian
	*/
	result_t XIMC_API set_stage_name (device_t id, const stage_name_t* stage_name);

/** 
	* \english
	* Read the user's stage name from the EEPROM.
	* @param id An identifier of a device
	* @param[out] stage_name structure contains the previously set user's stage name.
	* \endenglish
	* \russian
	* Чтение пользовательского имени подвижки из EEPROM.
	* @param id идентификатор устройства
	* @param[out] stage_name структура, содержащая установленное пользовательское имя позиционера
	* \endrussian
	*/
	result_t XIMC_API get_stage_name (device_t id, stage_name_t* stage_name);

/** 
	* \english
	* Deprecated. Set stage information to the EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] stage_information structure contains stage information
	* \endenglish
	* \russian
	* Запись информации о позиционере в EEPROM. Не поддерживается.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] stage_information структура, содержащая информацию о позиционере
	* \endrussian
	*/
	result_t XIMC_API set_stage_information (device_t id, const stage_information_t* stage_information);

/** 
	* \english
	* Deprecated. Read stage information from the EEPROM.
	* @param id An identifier of a device
	* @param[out] stage_information structure contains stage information
	* \endenglish
	* \russian
	* Чтение информации о позиционере из EEPROM. Не поддерживается.
	* @param id идентификатор устройства
	* @param[out] stage_information структура, содержащая информацию о позиционере
	* \endrussian
	*/
	result_t XIMC_API get_stage_information (device_t id, stage_information_t* stage_information);

/** 
	* \english
	* Deprecated. Set stage settings to the EEPROM.
	* Can be used by the manufacturer only
	* @param id An identifier of a device
	* @param[in] stage_settings structure contains stage settings
	* \endenglish
	* \russian
	* Запись настроек позиционера в EEPROM.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] stage_settings структура, содержащая настройки позиционера
	* \endrussian
	*/
	result_t XIMC_API set_stage_settings (device_t id, const stage_settings_t* stage_settings);

/**  
	* \english
	* Deprecated. Read stage settings from the EEPROM.
	* @param id An identifier of a device
	* @param[out] stage_settings structure contains stage settings
	* \endenglish
	* \russian
	* Чтение настроек позиционера из EEPROM.
	* @param id идентификатор устройства
	* @param[out] stage_settings структура, содержащая настройки позиционера
	* \endrussian
	*/
	result_t XIMC_API get_stage_settings (device_t id, stage_settings_t* stage_settings);

/** 
	* \english
	* Deprecated. Set motor information to the EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] motor_information structure contains motor information
	* \endenglish
	* \russian
	* Запись информации о двигателе в EEPROM.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] motor_information структура, содержащая информацию о двигателе
	* \endrussian
	*/
	result_t XIMC_API set_motor_information (device_t id, const motor_information_t* motor_information);

/**  
	* \english
	* Deprecated. Read motor information from the EEPROM.
	* @param id An identifier of a device
	* @param[out] motor_information structure contains motor information
	* \endenglish
	* \russian
	* Чтение информации о двигателе из EEPROM.
	* @param id идентификатор устройства
	* @param[out] motor_information структура, содержащая информацию о двигателе
	* \endrussian
	*/
	result_t XIMC_API get_motor_information (device_t id, motor_information_t* motor_information);

/** 
	* \english
	* Deprecated. Set motor settings to the EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] motor_settings structure contains motor information
	* \endenglish
	* \russian
	* Запись настроек двигателя в EEPROM.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] motor_settings структура, содержащая настройки двигателя
	* \endrussian
	*/
	result_t XIMC_API set_motor_settings (device_t id, const motor_settings_t* motor_settings);

/**  
	* \english
	* Deprecated. Read motor settings from the EEPROM.
	* @param id An identifier of a device
	* @param[out] motor_settings structure contains motor settings
	* \endenglish
	* \russian
	* Чтение настроек двигателя из EEPROM.
	* @param id идентификатор устройства
	* @param[out] motor_settings структура, содержащая настройки двигателя
	* \endrussian
	*/
	result_t XIMC_API get_motor_settings (device_t id, motor_settings_t* motor_settings);

/** 
	* \english
	* Deprecated. Set encoder information to the EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] encoder_information structure contains information about encoder
	* \endenglish
	* \russian
	* Запись информации об энкодере в EEPROM.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] encoder_information структура, содержащая информацию об энкодере
	* \endrussian
	*/
	result_t XIMC_API set_encoder_information (device_t id, const encoder_information_t* encoder_information);

/** 
	* \english
	* Deprecated. Read encoder information from the EEPROM.
	* @param id An identifier of a device
	* @param[out] encoder_information structure contains information about encoder
	* \endenglish
	* \russian
	* Чтение информации об энкодере из EEPROM.
	* @param id идентификатор устройства
	* @param[out] encoder_information структура, содержащая информацию об энкодере
	* \endrussian
	*/
	result_t XIMC_API get_encoder_information (device_t id, encoder_information_t* encoder_information);

/** 
	* \english
	* Deprecated. Set encoder settings to the EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] encoder_settings structure contains encoder settings
	* \endenglish
	* \russian
	* Запись настроек энкодера в EEPROM.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] encoder_settings структура, содержащая настройки энкодера
	* \endrussian
	*/
	result_t XIMC_API set_encoder_settings (device_t id, const encoder_settings_t* encoder_settings);

/** 
	* \english
	* Deprecated. Read encoder settings from the EEPROM.
	* @param id An identifier of a device
	* @param[out] encoder_settings structure contains encoder settings
	* \endenglish
	* \russian
	* Чтение настроек энкодера из EEPROM.
	* @param id идентификатор устройства
	* @param[out] encoder_settings структура, содержащая настройки энкодера
	* \endrussian
	*/
	result_t XIMC_API get_encoder_settings (device_t id, encoder_settings_t* encoder_settings);

/** 
	* \english
	* Deprecated. Set hall sensor information to the EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] hallsensor_information structure contains information about hall sensor
	* \endenglish
	* \russian
	* Запись информации о датчиках Холла в EEPROM.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] hallsensor_information структура, содержащая информацию о датчиках Холла
	* \endrussian
	*/
	result_t XIMC_API set_hallsensor_information (device_t id, const hallsensor_information_t* hallsensor_information);

/** 
	* \english
	* Deprecated. Read hall sensor information from the EEPROM.
	* @param id An identifier of a device
	* @param[out] hallsensor_information structure contains information about hall sensor
	* \endenglish
	* \russian
	* Чтение информации о датчиках Холла из EEPROM.
	* @param id идентификатор устройства
	* @param[out] hallsensor_information структура, содержащая информацию о датчиках Холла
	* \endrussian
	*/
	result_t XIMC_API get_hallsensor_information (device_t id, hallsensor_information_t* hallsensor_information);

/** 
	* \english
	* Deprecated. Set hall sensor settings to the EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] hallsensor_settings structure contains hall sensor settings
	* \endenglish
	* \russian
	* Запись настроек датчиков Холла в EEPROM.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] hallsensor_settings структура, содержащая настройки датчиков Холла
	* \endrussian
	*/
	result_t XIMC_API set_hallsensor_settings (device_t id, const hallsensor_settings_t* hallsensor_settings);

/** 
	* \english
	* Deprecated. Read hall sensor settings from the EEPROM.
	* @param id An identifier of a device
	* @param[out] hallsensor_settings structure contains hall sensor settings
	* \endenglish
	* \russian
	* Чтение настроек датчиков Холла из EEPROM.
	* @param id идентификатор устройства
	* @param[out] hallsensor_settings структура, содержащая настройки датчиков Холла
	* \endrussian
	*/
	result_t XIMC_API get_hallsensor_settings (device_t id, hallsensor_settings_t* hallsensor_settings);

/** 
	* \english
	* Deprecated. Set gear information to the EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] gear_information structure contains information about step gearhead
	* \endenglish
	* \russian
	* Запись информации о редукторе в EEPROM.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] gear_information структура, содержащая информацию о редукторе
	* \endrussian
	*/
	result_t XIMC_API set_gear_information (device_t id, const gear_information_t* gear_information);

/** 
	* \english
	* Deprecated. Read gear information from the EEPROM.
	* @param id An identifier of a device
	* @param[out] gear_information structure contains information about step gearhead
	* \endenglish
	* \russian
	* Чтение информации о редукторе из EEPROM.
	* @param id идентификатор устройства
	* @param[out] gear_information структура, содержащая информацию о редукторе
	* \endrussian
	*/
	result_t XIMC_API get_gear_information (device_t id, gear_information_t* gear_information);

/** 
	* \english
	* Deprecated. Set gear settings to the EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] gear_settings structure contains step gearhead settings
	* \endenglish
	* \russian
	* Запись настроек редуктора в EEPROM.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] gear_settings структура, содержащая настройки редуктора
	* \endrussian
	*/
	result_t XIMC_API set_gear_settings (device_t id, const gear_settings_t* gear_settings);

/** 
	* \english
	* Deprecated. Read gear settings from the EEPROM.
	* @param id An identifier of a device
	* @param[out] gear_settings structure contains step gearhead settings
	* \endenglish
	* \russian
	* Чтение настроек редуктора из EEPROM.
	* @param id идентификатор устройства
	* @param[out] gear_settings структура, содержащая настройки редуктора
	* \endrussian
	*/
	result_t XIMC_API get_gear_settings (device_t id, gear_settings_t* gear_settings);

/** 
	* \english
	* Deprecated. Set additional accessories' information to the EEPROM.
	* Can be used by the manufacturer only.
	* @param id An identifier of a device
	* @param[in] accessories_settings structure contains information about additional accessories
	* \endenglish
	* \russian
	* Запись информации о дополнительных аксессуарах в EEPROM.
	* Функция должна использоваться только производителем.
	* @param id идентификатор устройства
	* @param[in] accessories_settings структура, содержащая информацию о дополнительных аксессуарах
	* \endrussian
	*/
	result_t XIMC_API set_accessories_settings (device_t id, const accessories_settings_t* accessories_settings);

/** 
	* \english
	* Deprecated. Read additional accessory information from the EEPROM.
	* @param id An identifier of a device
	* @param[out] accessories_settings structure contains information about additional accessories
	* \endenglish
	* \russian
	* Чтение информации о дополнительных аксессуарах из EEPROM.
	* @param id идентификатор устройства
	* @param[out] accessories_settings структура, содержащая информацию о дополнительных аксессуарах
	* \endrussian
	*/
	result_t XIMC_API get_accessories_settings (device_t id, accessories_settings_t* accessories_settings);

/** 
	* \english
	* Read the controller's bootloader version.
	* @param id An identifier of a device
	* @param[out] Major major version
	* @param[out] Minor minor version
	* @param[out] Release release version
	* \endenglish
	* \russian
	* Чтение номера версии загрузчика контроллера.
	* @param id идентификатор устройства
	* @param[out] Major номер основной версии
	* @param[out] Minor номер дополнительной версии
	* @param[out] Release номер релиза
	* \endrussian
	*/
	result_t XIMC_API get_bootloader_version (device_t id, unsigned int* Major, unsigned int* Minor, unsigned int* Release);

/** 
	* \english
	* Read a random number from the controller. Manufacturer only.
	* @param id An identifier of a device
	* @param[out] init_random random sequence generated by the controller
	* \endenglish
	* \russian
	* Чтение случайного числа из контроллера. Только для производителя.
	* @param id идентификатор устройства
	* @param[out] init_random случайная последовательность, сгенерированная контроллером
	* \endrussian
	*/
	result_t XIMC_API get_init_random (device_t id, init_random_t* init_random);

/** 
  * \english
  * This value is unique to each individual device, but is not a random value. Manufacturer only.
  * This unique device identifier can be used to initiate secure boot processes or as a serial number for USB or other end applications.
  * @param id An identifier of a device
  * @param[out] globally_unique_identifier the result of fields 0-3 concatenated defines the unique 128-bit device identifier.
  * \endenglish
  * \russian
  * Считывает уникальный идентификатор каждого чипа, это значение не является случайным. Только для производителя.
  * Уникальный идентификатор может быть использован в качестве инициализационного вектора
  * для операций шифрования бутлоадера или в качестве серийного номера для USB и других применений.
  * @param id идентификатор устройства
  * @param[out] globally_unique_identifier результат полей 0-3 определяет уникальный 128-битный идентификатор.
  * \endrussian
  */
	result_t XIMC_API get_globally_unique_identifier (device_t id, globally_unique_identifier_t* globally_unique_identifier);


/*
 -------------------------
   END OF GENERATED CODE
 -------------------------
*/

/* hand-crafted functions begin */

	/**
		* \english
		* Reboot to firmware
		* @param id an identifier of device
		* @param[out] ret RESULT_OK, if reboot to firmware is possible. Reboot is done after reply to this command. RESULT_NO_FIRMWARE, if firmware is not found. RESULT_ALREADY_IN_FIRMWARE, if this command was sent when controller is already in firmware.
		* \endenglish
		* \russian
		* Перезагрузка в прошивку в контроллере
		* @param id идентификатор устройства
		* @param[out] ret RESULT_OK, если переход из загрузчика в прошивку возможен. После ответа на эту команду выполняется переход. RESULT_NO_FIRMWARE, если прошивка не найдена. RESULT_ALREADY_IN_FIRMWARE, если эта команда была вызвана из прошивки.
		* \endrussian
		*/
	result_t XIMC_API goto_firmware(device_t id, uint8_t* ret);

	/**
		* \english
		* Check for firmware on device
		* @param uri a uri of device
		* @param[out] ret non-zero if firmware existed
		* \endenglish
		* \russian
		* Проверка наличия прошивки в контроллере
		* @param uri уникальный идентификатор ресурса устройства
		* @param[out] ret ноль, если прошивка присутствует
		* \endrussian
		*/
	result_t XIMC_API has_firmware(const char* uri, uint8_t* ret);

	/**
		* \english
		* Update firmware. Manufacturer only.
		* Service command
		* @param uri a uri of device
		* @param data firmware byte stream
		* @param data_size size of byte stream
		* \endenglish
		* \russian
		* Обновление прошивки. Команда только для производителя.
		* @param uri идентификатор устройства
		* @param data указатель на массив байтов прошивки
		* @param data_size размер массива в байтах
		* \endrussian
		*/
	result_t XIMC_API command_update_firmware(const char* uri, const uint8_t* data, uint32_t data_size);

/**
	* \english
	* Write controller key.
	* Can be used by manufacturer only
	* @param uri a uri of device
	* @param[in] key protection key. Range: 0..4294967295
	* \endenglish
	* \russian
	* Запись ключа защиты
	* Функция используется только производителем.
	* @param uri идентификатор устройства
	* @param[in] key ключ защиты. Диапазон: 0..4294967295
	* \endrussian
	*/
	result_t XIMC_API write_key (const char* uri, uint8_t* key);

/**
	* \english
	* Reset controller.
	* Can be used by manufacturer only
	* @param id an identifier of device
	* \endenglish
	* \russian
	* Перезагрузка контроллера.
	* Функция используется только производителем.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_reset(device_t id);

/**
	* \english
	* Clear controller FRAM.
	* Can be used by manufacturer only
	* @param id an identifier of device
	* \endenglish
	* \russian
	* Очистка FRAM памяти контроллера.
	* Функция используется только производителем.
	* @param id идентификатор устройства
	* \endrussian
	*/
	result_t XIMC_API command_clear_fram(device_t id);

	//@}

	// ------------------------------------

	/**
		\english
		@name Boards and drivers control
		* Functions for searching and opening/closing devices
		\endenglish
		\russian
		@name Управление устройством
		* Функции поиска и открытия/закрытия устройств
		\endrussian
		*/
	//@{

	/**
		* \english
		* Open a device with OS \a uri and return identifier of the device which can be used in calls.
		* @param[in] uri - a device URI.
		* Device URI has a form of "xi-com:port" or "xi-net://host/serial" or "xi-emu:///abs_path_to_file". For POSIX systems one can ommit root-slash in abs_path_to_file; for example, "xi-emu:///home/user/virt_controller.bin". 
		* In case of USB-COM port, the "port" is the OS device URI. For example, "xi-com:\\\\\.\\COM3" in Windows (note that double-backslash will be transformed to single-backslash) or "xi-com:///dev/ttyACM0" in Linux/Mac.
		* In case of network device, the "host" is an IPv4 address or fully qualified domain URI (FQDN), "serial" is the device serial number in hexadecimal system.
		* For example, "xi-net://192.168.0.1/00001234" or "xi-net://hostname.com/89ABCDEF".
		* In case of TCP protocol, use "xi-tcp://<ip/host>:<port>.
		* For example, "xi-tcp://192.168.0.1:1818".
		* In case of virtual device, the "abs_file_to_file" is the full path to the virtual device's file. If it doesn't exist, then it is created and initialized with default values.
		* For example, "xi-emu:///C:/dir/file.bin" in Windows or "xi-emu:///home/user/file.bin" in Linux/Mac.
		* \endenglish
		* \russian
		* Открывает устройство по имени \a uri и возвращает идентификатор, который будет использоваться для обращения к устройству.
		* @param[in] uri - уникальный идентификатор устройства.
		* URI устройства имеет вид "xi-com:port" или "xi-net://host/serial" или "xi-emu:///abs_path_to_file". На POSIX системах допускается пропуск "рутовского" слэша; например, "xi-emu:///home/user/virt_controller.bin".
		* Для USB-COM устройства "port" это URI устройства в ОС.
		* Например, "xi-com:\\\\\.\\COM3" в Windows (с учётом экранирования двойные обратные слэши преобразуются в одинарные) или "xi-com:///dev/ttyACM0" в Linux/Mac.
		* Для сетевого устройства "host" это IPv4 адрес или полностью определённое имя домена, "serial" это серийный номер устройства в шестнадцатеричной системе.
		* Например, "xi-net://192.168.0.1/00001234" или "xi-net://hostname.com/89ABCDEF".
		* Для работы по TCP протоколу используйте "xi-tcp://<ip/host>:<port>.
		* Например, "xi-tcp://192.168.0.1:1818".
		* Для виртуального устройства "abs_file_to_file" это путь к файлу с сохраненным состоянием устройства. Если файл не существует, он будет создан и инициализирован значениями по умолчанию.
		* Например, "xi-emu:///C:/dir/file.bin" в Windows или "xi-emu:///home/user/file.bin" в Linux/Mac.
		* \endrussian
		*/
	device_t XIMC_API open_device (const char* uri);

	/**
		* \english
		* Close specified device
		* @param id an identifier of device
		* \note
		* The id parameter in this function is a C pointer, unlike most library functions that use this parameter
		* \endenglish
		* \russian
		* Закрывает устройство
		* @param id - идентификатор устройства
		* \note
		* Параметр id в данной функции является Си указателем, в отличие от большинства функций библиотеки использующих данный параметр
		* \endrussian
		*/
	result_t XIMC_API close_device (device_t* id);

	/**
		* \english
		* Command of loading a correction table from a text file (this function is deprecated).
		* Use the function set_correction_table(device_t id, const char* namefile).
		* The correction table is used for position correction in case of mechanical inaccuracies.
		* It works for some parameters in _calb commands.
		* @param id an identifier the device
		* @param[in] namefile - the file name must be fully qualified.
		* If the short name is used, the file must be located in the application directory.
		* If the file name is set to NULL, the correction table will be cleared.
		* File format: two tab-separated columns.
		* Column headers are string.
		* Data is real, the point is a determiter.
		* The first column is a coordinate. The second one is the deviation caused by a mechanical error.
		* The maximum length of a table is 100 rows.
		* \note
		* The id parameter in this function is a C pointer, unlike most library functions that use this parameter
		* @see command_move
		* @see get_position_calb
		* @see get_position_calb_t
		* @see get_status_calb
		* @see status_calb_t
		* @see get_edges_settings_calb
		* @see set_edges_settings_calb
		* @see edges_settings_calb_t
		*
		* \endenglish
		* \russian
		* Команда загрузки корректирующей таблицы из текстового файла (данная функция устарела).
		* Используйте функцию set_correction_table(device_t id, const char* namefile).
		* Таблица используется для коррекции положения в случае механических неточностей.
		* Работает для некоторых параметров в _calb командах.
		* @param id - идентификатор устройства
		* @param[in] namefile - имя файла должно быть полным.
		* Если используется короткое имя, файл должен находится в директории приложения.
		* Если имя файла равно NULL таблица коррекции будет очищена.
		* Формат файла: два столбца разделенных табуляцией.
		* Заголовки столбцов строковые.
		* Данные действительные разделитель точка.
		* Первый столбец координата. Второй - отклонение вызванное ошибкой механики.
		* Между координатами отклонение расчитывается линейно. За диапазоном константа равная отклонению на границе.
		* Максимальная длина таблицы 100 строк.		
		* \note
		* Параметр id в данной функции является Си указателем, в отличие от большинства функций библиотеки использующих данный параметр
		* @see command_move
		* @see command_movr
		* @see get_position_calb
		* @see get_position_calb_t
		* @see get_status_calb
		* @see status_calb_t
		* @see get_edges_settings_calb
		* @see set_edges_settings_calb
		* @see edges_settings_calb_t
		* \endrussian
		*/
	result_t XIMC_API load_correction_table(device_t* id, const char* namefile);

	/**
	* \english
	* Command of loading a correction table from a text file.
	* The correction table is used for position correction in case of mechanical inaccuracies.
	* It works for some parameters in _calb commands.
	* @param id an identifier the device
	* @param[in] namefile - the file name must be either a full path or a relative path. If the file name is set to NULL,
	* the correction table will be cleared. File format: two tab-separated columns. Column headers are strings.
	* Data is real, the dot is a delimiter. The first column is a coordinate. The second one is the deviation
	* caused by a mechanical error. The maximum length of a table is 100 rows. Coordinate column must be sorted in
	* ascending order.
	* @see command_move
	* @see get_position_calb
	* @see get_position_calb_t
	* @see get_status_calb
	* @see status_calb_t
	* @see get_edges_settings_calb
	* @see set_edges_settings_calb
	* @see edges_settings_calb_t
	*
	* \endenglish
	* \russian
	* Команда загрузки корректирующей таблицы из текстового файла.
	* Таблица используется для коррекции положения в случае механических неточностей.
	* Работает для некоторых параметров в _calb командах.
	* @param id - идентификатор устройства
	* @param[in] namefile - путь до файла должен быть полным или относительным.
	* Если параметр равен NULL, таблица коррекции будет очищена.
	* Формат файла: два столбца, разделенных табуляцией.
	* Заголовки столбцов строковые.
	* Данные действительные, разделитель точка.
	* Первый столбец - координата. Второй - отклонение, вызванное ошибкой механики.
	* Максимальная длина таблицы 100 строк. Координаты должны быть отсортированы по возрастанию.
	* @see command_move
	* @see command_movr
	* @see get_position_calb
	* @see get_position_calb_t
	* @see get_status_calb
	* @see status_calb_t
	* @see get_edges_settings_calb
	* @see set_edges_settings_calb
	* @see edges_settings_calb_t
	* \endrussian
	*/
	result_t XIMC_API set_correction_table(device_t id, const char* namefile);

	/**
		* \english
		* Check if a device with OS uri \a uri is XIMC device.
		* Be carefuly with this call because it sends some data to the device.
		* @param[in] uri - a device uri
		* \endenglish
		* \russian
		* Проверяет, является ли устройство с уникальным идентификатором \a uri XIMC-совместимым.
		* Будьте осторожны с вызовом этой функции для неизвестных устройств, т.к. она отправляет данные.
		* @param[in] uri - уникальный идентификатор устройства
		* \endrussian
		*/
	result_t XIMC_API probe_device (const char* uri);

	/**
		* \english
		* Deprecated. Left for compatibility. Do just nothing.
		* \endenglish
		* \russian
		* Устарело. Оставлено для совместимости. Ничего не делает.
		* \endrussian
    */
	result_t XIMC_API set_bindy_key(const char* keyfilepath);

	/**
		* \english
		* Search and list of available devices. By default, it creates a list of devices connected to this computer and presented as COM ports.
		* Additionally, you can enable the search for network devices. Devices found in the local network will be included in the same list.<br>
		* To obtain information from the collected list, use the corresponding functions with the <code>get_enumerate_</code> prefix and
		* the received <code>device_enumeration</code> identifier.<br>
		* After finishing working with the list of found devices, you should free up memory using the <code>free_enumerate_devices()</code> function.
		* @param[in] enumerate_flags a set of flags that specify search modes. Flags can be used together via bitwise "OR":
		* <ul>
		* <li><code>ENUMERATE_NETWORK</code> - enables searching for network devices. If the flag is set, network devices will be added to the general list.
		* If the flag is not set, the list will only include devices connected to this computer.</li>
		* <li><code>ENUMERATE_ALL_COM</code> - when enabled, queries all COM port devices in the system. When disabled, queries only devices whose names
		* match the XIMC device mask ("XIMC Motor Controller" in Windows, <code>/dev/ximc/</code> and <code>/dev/ttyACM/</code> on Linux/Mac).</li>
		* <li><code>ENUMERATE_PROBE</code> - enables checking of devices and collecting additional information (serial number, version, model, name...).
		* If this flag is set, only devices that are guaranteed to be open will be added to the list, but devices connected via RS232 converters may not be included.
		* If the flag is not set, the list will include more devices (in particular, devices explicitly listed in <code>hints</code> will be included),
		* but availability and compatibility with this library is not guaranteed.</li>
		* </ul>
		* @param[in] hints additional information to improve the search efficiency. It makes sense to use in case of complex network configuration,
		* when automatic search may not find everything. Format - string <code>"key1=value1\nkey2=value2"</code>. Unknown keys are ignored.
		* One key can have several values, which are listed separated by commas: <code>key=value1,value2,value3</code>. Valid keys:
		* <ul>
		* <li><code>addr</code> - list of URLs of network controllers or servers with connected controllers. The field is used together with the <code>ENUMERATE_NETWORK</code> flag.
		* Protocols and address formats:
		* <ul>
		* <li><code>xi-tcp://&lt;ip-address&gt; - network controllers and controllers connected via Ethernet-RS232 converters. If the <code>ENUMERATE_PROBE</code>
		* flag is not set, all listed devices will be included in the list.</li>
		* <li>xi-net://&lt;ip-address&gt; - network multi-axis systems, xi-net servers. The request for information about the availability of controllers at these
		* addresses will be made regardless of the results of the automatic network search procedure.</li>
		* </ul>
		* </li>
		* <li><code>adapter_addr</code> - list of IP addresses of local network adapters through which the search should be performed.
		* If the key is missing or no adapters are specified, the search is performed on all adapters.</li>
		* </ul>
		* \return <code>device_enumeration</code> - device list identifier. Used to obtain information about devices using functions with <code>get_enumerate_</code> prefixes.
		* \par Examples of use:
		* <pre>
		* <code>
		* // Search for local devices without checking
		* device_enumeration_t device_enumeration = enumerate_devices(0, "");
		* // Search for local devices with verification
		* device_enumeration_t device_enumeration = enumerate_devices(ENUMERATE_PROBE, "");
		* // Fully automatic search for local and network devices
		* device_enumeration_t device_enumeration = enumerate_devices(ENUMERATE_NETWORK, "");
		* // Search for local and network devices
		* // using the local computer adapter with the address 192.168.0.100
		* // and explicit requests to the network controller with the address 192.168.0.11
		* // and the xi-net server with the address 192.168.0.10
		* device_enumeration_t device_enumeration = enumerate_devices(
		* ENUMERATE_NETWORK,
		* "addr=192.168.0.10,xi-tcp://192.168.0.11\nadapter_addr=192.168.0.100" 
		* ); 
		* </code>
		* </pre>
		* \endenglish
		* \russian
		* Поиск и составление списка доступных устройств. По умолчанию формирует список устройств, подключенных к данному компьютеру и представленных в виде COM-портов. 
		* Дополнительно можно включить поиск сетевых устройств. Найденные в локальной сети устройства попадут в тот же список.<br>
		* Для получения информации из собранного списка воспользуйтесь соответствующими функциями с префиксом <code>get_enumerate_</code> и
		* полученным идентификатором <code>device_enumeration</code>.<br>
		* После завершения работы со списком найденных устройств следует освободить память с помощью функции <code>free_enumerate_devices()</code>.
		* @param[in] enumerate_flags набор флагов, задающих режимы поиска. Флаги могут применяться совместно через побитовое "ИЛИ".
		* <ul>
		* <li><code>ENUMERATE_NETWORK</code> - включает поиск сетевых устройств. Если флаг установлен, сетевые устройства будут добавлены в общий список.
		* Если флаг не установлен, в списке будут только устройства, подключенные к данному компьютеру.</li>
		* <li><code>ENUMERATE_ALL_COM</code> - при включенной опции опрашивает все устройства типа COM-порт в системе. При отключенной опции опрашивает только устройства,
		* имена которых соответствуют маске устройств XIMC ("XIMC Motor Controller" в Windows, <code>/dev/ximc/</code> и <code>/dev/ttyACM/</code> на Linux/Mac).</li>
		* <li><code>ENUMERATE_PROBE</code> - включает проверку устройств и сбор дополнительной информации (серийный номер, версию, модель, имя...).
		* Если данный флаг установлен, в список будут добавлены только устройства, которые гарантированно можно открыть, но могут не попадать устройства,
		* подключенные через RS232-преобразователи. Если флаг не установлен, то в списке будет больше устройств (в частности, попадут устройства,
		* явным образом перечисленные в <code>hints</code>), но доступность и совместимость с данной библиотекой не гарантируется.</li>
		* </ul>
		* @param[in] hints дополнительная информация для повышения эффективности поиска. Имеет смысл использовать в случае сложной сетевой конфигурации,
		* когда автоматический поиск может находить не всё. Формат - строка <code>"ключ1=значение1\nключ2=значение2"</code>. Неизвестные ключи игнорируются.
		* Один ключ может иметь несколько значений, которые перечисляются через запятую: <code>ключ=значение1,значение2,значение3</code>. Допустимые ключи:
		* <ul>
		* <li><code>addr</code> - список URLов сетевых контроллеров или серверов с подключенными контроллерами. Поле применяется совместно с флагом <code>ENUMERATE_NETWORK</code>.
		* Протоколы и форматы адресов:
		* <ul>
		* <li><code>xi-tcp://&lt;ip-адрес&gt;</code> - сетевые контроллеры и контроллеры, подключенные через Ethernet-RS232 преобразователи.
		* Если флаг <code>ENUMERATE_PROBE</code> не установлен, все перечисленные устройства попадут в список.</li>
		* <li><code>xi-net://&lt;ip-адрес&gt;</code> - сетевые многоосевые системы, xi-net сервера. Запрос информации о наличии контроллеров по этим адресам будет сделан
		* независимо от результатов автоматической процедуры сетевого поиска.</li>
		* </ul>
		* </li>
		* <li><code>adapter_addr</code></li> - список IP-адресов локальных сетевых адаптеров, через который должен осуществляться поиск. Если ключ отсутствует или ни одного адаптера не указано,
		* то поиск производится на всех адаптерах.</li>
		* </ul>
		* \return <code>device_enumeration</code> - идентификатор списка устройств. Используется для получения информации об устройствах с помощью функций с префиксами <code>get_enumerate_</code>.<br>
		* \par Примеры использования:
		* <pre>
		* <code>
		* // Поиск локальных устройств без проверки
		* device_enumeration_t device_enumeration = enumerate_devices(0, ""); 
		* // Поиск локальных устройств c проверкой
		* device_enumeration_t device_enumeration = enumerate_devices(ENUMERATE_PROBE, "");
		* // Полностью автоматический поиск локальных и сетевых устройств
		* device_enumeration_t device_enumeration = enumerate_devices(ENUMERATE_NETWORK, "");
		* // Поиск локальных и сетевых устройств
		* // с использованием адаптера локального компьютера с адресом 192.168.0.100
		* // и явным обращениям к сетевому контроллеру с адресом 192.168.0.11
		* // и xi-net серверу с адресом  192.168.0.10
		* device_enumeration_t device_enumeration = enumerate_devices(
		* ENUMERATE_NETWORK,
		* "addr=192.168.0.10,xi-tcp://192.168.0.11\nadapter_addr=192.168.0.100"
		* );
		* </code>
		* </pre>
		* \endrussian
	 */
	device_enumeration_t XIMC_API enumerate_devices(int enumerate_flags, const char *hints);

	/**
		* \english
		* Free memory returned by \a enumerate_devices.
		* @param[in] device_enumeration opaque pointer to an enumeration device data
		* \endenglish
		* \russian
		* Освобождает память, выделенную \a enumerate_devices.
		* @param[in] device_enumeration закрытый указатель на данные о перечисленных устойствах
		* \endrussian
	 */
	result_t XIMC_API free_enumerate_devices(device_enumeration_t device_enumeration);

	/**
		* \english
		* Get device count.
		* @param[in] device_enumeration opaque pointer to an enumeration device data
		* \endenglish
		* \russian
		* Возвращает количество подключенных устройств.
		* @param[in] device_enumeration закрытый указатель на данные о перечисленных устойствах
		* \endrussian
	 */
	int XIMC_API get_device_count(device_enumeration_t device_enumeration);

	/**
		* \english
		* Nevermind
		* \endenglish
		* \russian
		* Не обращайте на меня внимание
		* \endrussian
	*/
	typedef char* pchar;

	/**
		* \english
		* Get device name from the device enumeration.
		* Returns \a device_index device name.
		* @param[in] device_enumeration opaque pointer to an enumeration device data
		* @param[in] device_index device index
		* \endenglish
		* \russian
		* Возвращает имя подключенного устройства из перечисления устройств.
		* Возвращает имя устройства с номером \a device_index.
		* @param[in] device_enumeration закрытый указатель на данные о перечисленных устойствах
		* @param[in] device_index номер устройства
		* \endrussian
	 */
	pchar XIMC_API get_device_name(device_enumeration_t device_enumeration, int device_index);


	/**
		* \english
		* Get device serial number from the device enumeration.
		* Returns \a device_index device serial number.
		* @param[in] device_enumeration opaque pointer to an enumeration device data
		* @param[in] device_index device index
		* @param[out] serial device serial number
		* \endenglish
		* \russian
		* Возвращает серийный номер подключенного устройства из перечисления устройств.
		* Возвращает серийный номер устройства с номером \a device_index.
		* @param[in] device_enumeration закрытый указатель на данные о перечисленных устойствах
		* @param[in] device_index номер устройства
		* @param[in] serial серийный номер устройства
		* \endrussian
	 */
	result_t XIMC_API get_enumerate_device_serial(device_enumeration_t device_enumeration, int device_index, uint32_t* serial);

	/**
		* \english
		* Get device information from the device enumeration.
		* Returns \a device_index device information.
		* @param[in] device_enumeration opaque pointer to an enumeration device data
		* @param[in] device_index device index
		* @param[out] device_information device information data
		* \endenglish
		* \russian
		* Возвращает информацию о подключенном устройстве из перечисления устройств.
		* Возвращает информацию о устройстве с номером \a device_index.
		* @param[in] device_enumeration закрытый указатель на данные о перечисленных устойствах
		* @param[in] device_index номер устройства
		* @param[out] device_information информация об устройстве
		* \endrussian
	 */
	result_t XIMC_API get_enumerate_device_information(device_enumeration_t device_enumeration, int device_index, device_information_t* device_information);

	/**
		* \english
		* Get controller name from the device enumeration.
		* Returns \a device_index device controller name.
		* @param[in] device_enumeration opaque pointer to an enumeration device data
		* @param[in] device_index device index
		* @param[out] controller_name controller name
		* \endenglish
		* \russian
		* Возвращает имя подключенного устройства из перечисления устройств.
		* Возвращает имя устройства с номером \a device_index.
		* @param[in] device_enumeration закрытый указатель на данные о перечисленных устойствах
		* @param[in] device_index номер устройства
		* @param[out] controller name имя устройства
		* \endrussian
	 */
	result_t XIMC_API get_enumerate_device_controller_name(device_enumeration_t device_enumeration, int device_index, controller_name_t* controller_name);

	/**
		* \english
		* Get stage name from the device enumeration.
		* Returns \a device_index device stage name.
		* @param[in] device_enumeration opaque pointer to an enumeration device data
		* @param[in] device_index device index
		* @param[out] stage_name stage name
		* \endenglish
		* \russian
		* Возвращает имя подвижки для подключенного устройства из перечисления устройств.
		* Возвращает имя подвижки устройства с номером \a device_index.
		* @param[in] device_enumeration закрытый указатель на данные о перечисленных устойствах
		* @param[in] device_index номер устройства
		* @param[out] stage name имя подвижки
		* \endrussian
	 */
	result_t XIMC_API get_enumerate_device_stage_name(device_enumeration_t device_enumeration, int device_index, stage_name_t* stage_name);

	/**
		* \english
		* Get device network information from the device enumeration.
		* Returns \a device_index device network information.
		* @param[in] device_enumeration opaque pointer to an enumeration device data
		* @param[in] device_index device index
		* @param[out] device_network_information device network information data
		* \endenglish
		* \russian
		* Возвращает сетевую информацию о подключенном устройстве из перечисления устройств.
		* Возвращает сетевую информацию о устройстве с номером \a device_index.
		* @param[in] device_enumeration закрытый указатель на данные о перечисленных устойствах
		* @param[in] device_index номер устройства
		* @param[out] device_network_information сетевая информация об устройстве
		* \endrussian
	 */
	result_t XIMC_API get_enumerate_device_network_information(device_enumeration_t device_enumeration, int device_index, device_network_information_t* device_network_information);

	/** \english
		* Resets the error of incorrect data transmission.
		* \endenglish
		* \russian
		* Сбрасывает ошибку неправильной передачи данных.
		* \endrussian
	*/
	result_t XIMC_API reset_locks ();

	/** \english
		* (Deprecated) Fixing a USB driver error in Windows.
		* The USB-COM subsystem in the Windows OS does not always work correctly. During operation, the following malfunctions are possible:
		* All attempts to open the device fail. The device can be opened and data can be sent to it, but the response data is not received.
		* These problems are fixed by reconnecting the device or reinitializing it in the Device Manager.
		* The ximc_fix_usbser_sys() function automates the deletion detection process.
		* \endenglish
		* \russian
		* (Устарела) Исправление ошибки драйвера USB в Windows.
		* Подсистема USB-COM на OC Windows не всегда работает корректно. При работе возможны следующие неисправности:
		* Все попытки открыть устройство заканчиваются неудачно. Устройство можно открыть и отправить в него данные, но ответные данные не приходят.
		* Эти проблемы исправляются переподключением устройства или его переинециализацией в диспетчере устройств.
		* Функция ximc_fix_usbser_sys() автоматизирует процесс удаления-обнаружения.
		* \endrussian
		*/
	result_t XIMC_API ximc_fix_usbser_sys(const char* device_uri);


	/** \english
		* Sleeps for a specified amount of time
		* @param msec time in milliseconds
		* \endenglish
		* \russian
		* Приостанавливает работу на указанное время
		* @param msec время в миллисекундах
		* \endrussian
		*/
	void XIMC_API msec_sleep (unsigned int msec);

	/** \english
		* Returns a library version
		* @param version a buffer to hold a version string, 32 bytes is enough
		* \endenglish
		* \russian
		* Возвращает версию библиотеки
		* @param version буфер для строки с версией, 32 байт достаточно
		* \endrussian
		*/
	void XIMC_API ximc_version (char* version);

#if !defined(MATLAB_IMPORT) && !defined(LABVIEW64_IMPORT) && !defined(LABVIEW32_IMPORT)

	/** \english
		* Logging callback prototype
		* @param loglevel a loglevel
		* @param message a message
		* \endenglish
		* \russian
		* Прототип функции обратного вызова для логирования
		* @param loglevel уровень логирования
		* @param message сообщение
		* \endrussian
		*/
	typedef void (XIMC_CALLCONV *logging_callback_t)(int loglevel, const wchar_t* message, void* user_data);

	/** \english
		* Simple callback for logging to stderr in wide chars
		* @param loglevel a loglevel
		* @param message a message
		* \endenglish
		* \russian
		* Простая функция логирования на stderr в широких символах
		* @param loglevel уровень логирования
		* @param message сообщение
		* \endrussian
		*/
	void XIMC_API logging_callback_stderr_wide(int loglevel, const wchar_t* message, void* user_data);

	/** \english
		* Simple callback for logging to stderr in narrow (single byte) chars
		* @param loglevel a loglevel
		* @param message a message
		* \endenglish
		* \russian
		* Простая функция логирования на stderr в узких (однобайтных) символах
		* @param loglevel уровень логирования
		* @param message сообщение
		* \endrussian
		*/
	void XIMC_API logging_callback_stderr_narrow(int loglevel, const wchar_t* message, void* user_data);

	/**
		* \english
		* Sets a logging callback.
		* Call resets a callback to default (stderr, syslog) if NULL passed.
		* @param logging_callback a callback for log messages
		* \endenglish
		* \russian
		* Устанавливает функцию обратного вызова для логирования.
		* Вызов назначает стандартный логгер (stderr, syslog), если передан NULL
		* @param logging_callback указатель на функцию обратного вызова
		* \endrussian
		*/
	void XIMC_API set_logging_callback(logging_callback_t logging_callback, void* user_data);

#endif

/**
	* \english
	* Return device state.
	* @param id an identifier of device
	* @param[out] status structure with snapshot of controller status
	* \endenglish
	* \russian
	* Возвращает информацию о текущем состоянии устройства.
	* @param id идентификатор устройства
	* @param[out] status структура с информацией о текущем состоянии устройства
	* \endrussian
	*/
/**
	* \english
	* Device state.
	* Useful structure that contains current controller status, including speed, position and boolean flags.
	* \endenglish
	* \russian
	* Состояние устройства.
	* Эта структура содержит основные параметры текущего состояния контроллера, такие как скорость, позиция и флаги состояния.
	* \endrussian
	* @see get_status
	*/
	result_t XIMC_API get_status (device_t id, status_t* status);

/**
	* \english
	* Return device state.
	* @param id an identifier of device
	* @param[out] status structure with snapshot of controller status
	* @param calibration user unit settings
	* \endenglish
	* \russian
	* Возвращает информацию о текущем состоянии устройства.
	* @param id идентификатор устройства
	* @param[out] status структура с информацией о текущем состоянии устройства
	* @param calibration настройки пользовательских единиц
	* \endrussian
	*/
/**
	* \english
	* Calibrated device state.
	* Useful structure that contains current controller status, including speed, position and boolean flags.
	* \endenglish
	* \russian
	* Состояние устройства в калиброванных единицах.
	* Эта структура содержит основные параметры текущего состояния контроллера, такие как скорость, позиция и флаги состояния, размерные величины выводятся в калиброванных единицах.
	* \endrussian
	* @see get_status
	*/
	result_t XIMC_API get_status_calb (device_t id, status_calb_t* status, const calibration_t* calibration);

/**
	* \english
	* Return device information.
	* All fields must point to allocated string buffers with at least 10 bytes.
	* Works with both raw or initialized device.
	* @param id an identifier of device
	* @param[out] device_information device information
	* \endenglish
	* \russian
	* Возвращает информацию об устройстве.
	* Все входные параметры должны быть указателями на выделенные области памяти длиной не менее 10 байт.
	* Команда доступна как из инициализированного состояния, так и из исходного.
	* @param id идентификатор устройства.
	* @param[out] device_information информация об устройстве
	* \endrussian
	*/
/**
	* \english
	* Device information.
	* \endenglish
	* \russian
	* Информация об устройстве.
	* \endrussian
	* @see get_device_information
	*/
	result_t XIMC_API get_device_information (device_t id, device_information_t* device_information);

/**
	* \english
	* Wait for stop
	* @param id an identifier of device
	* @param refresh_interval_ms Status refresh interval. The function waits this number of milliseconds between get_status requests to the controller.
	* Recommended value of this parameter is 10 ms. Use values of less than 3 ms only when necessary - small refresh interval values do not significantly
	* increase response time of the function, but they create substantially more traffic in controller-computer data channel.
	* @param[out] ret RESULT_OK if controller has stopped and result of the first get_status command which returned anything other than RESULT_OK otherwise.
	* \endenglish
	* \russian
	* Ожидание остановки контроллера
	* @param id идентификатор устройства
	* @param refresh_interval_ms Интервал обновления. Функция ждет столько миллисекунд между отправками контроллеру запроса get_status для проверки статуса остановки.
	* Рекомендуемое значение интервала обновления - 10 мс. Используйте значения меньше 3 мс только если это необходимо - малые значения интервала обновления
	* незначительно ускоряют обнаружение остановки, но создают существенно больший поток данных в канале связи контроллер-компьютер.
	* @param[out] ret RESULT_OK, если контроллер остановился, в противном случае первый результат выполнения команды get_status со статусом отличным от RESULT_OK.
	* \endrussian
	*/
	result_t XIMC_API command_wait_for_stop(device_t id, uint32_t refresh_interval_ms);
	
	/**
	* \english
	* Make home command, wait until it is finished and make zero command. This is a convinient way to calibrate zero position.
	* @param id an identifier of device
	* @param[out] ret RESULT_OK if controller has finished home & zero correctly or result of first controller query that returned anything other than RESULT_OK.
	* \endenglish
	* \russian
	* Запустить процедуру поиска домашней позиции, подождать её завершения и обнулить позицию в конце. Это удобный путь для калибровки нулевой позиции.
	* @param id идентификатор устройства
	* @param[out] ret RESULT_OK, если контроллер завершил выполнение home и zero корректно или результат первого запроса к контроллеру со статусом отличным от RESULT_OK.
	* \endrussian
	*/
	result_t XIMC_API command_homezero(device_t id);
	//@}

#if defined(__cplusplus)
};
#endif

#endif

// vim: ts=4 shiftwidth=4

