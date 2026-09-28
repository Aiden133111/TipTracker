import sys
import traceback
from dataclasses import dataclass, field
from enum import Enum
from opentrons import protocol_api
from opentrons.protocol_api.labware import OutOfTipsError
from opentrons.protocol_api import ALL, COLUMN, SINGLE, ROW, PARTIAL_COLUMN
from opentrons.types import NozzleConfigurationType

#PROTOCOL REQUIREMENTS
metadata = {
	'protocolName': 'Tip Tracking Class',
	'author': 'Aiden McFadden',
	'source': 'https://github.com/Aiden133111/TipTracker',
	'description': 'TipTracker V2: track tips across deck, expansion, adapters, and stackers',
}

requirements = {
	"robotType": "Flex",
	"apiLevel": "2.27", #Min tested version posted here
}


class DeckRegion(str, Enum):
	'''Where a tip rack lives relative to the pipetting grid (V2 internal model).'''
	MAIN = 'main'
	EXPANSION = 'expansion'
	ADAPTER = 'adapter'


@dataclass
class StackerSupply:
	'''One Flex stacker module holding a tip type (V2 replaces opaque ``[module, count, lid]`` rows).'''
	module: protocol_api.FlexStackerContext
	rack_count: int
	has_lid: bool

	@classmethod
	def from_legacy_row(cls, row: list) -> 'StackerSupply':
		return cls(module=row[0], rack_count=row[1] if row[1] is not None else 0, has_lid=bool(row[2]))

	def to_legacy_row(self) -> list:
		return [self.module, self.rack_count, self.has_lid]


@dataclass
class RefillSnapshot:
	'''
	Snapshot of main-deck exhaustion and shuttle targets when ``pick_up`` hits OutOfTipsError.
	Built once per refill attempt so expansion/stacker/manual paths share the same checks.
	'''
	tip_load_name: str
	slots_to_check: list[str]
	vacant_slots: list[str]
	empty_tipracks: list
	empty_tiprack_slots: list[str]
	deposit_slots: list[str]
	wasted_slot_ids: list[str] = field(default_factory=list)
	stacker_carousel_olds: list = field(default_factory=list)
	other_rack_slots: dict = field(default_factory=dict)
	empty_tip_slots: dict = field(default_factory=dict)
	layout: tuple | None = None


class TipTracker:
	'''Track tipracks across deck, expansion, adapters, and stackers; auto-refill on OutOfTipsError.
	Refill priority: assigned slots → pick_up_slots shuffle → expansion → stacker → manual.
	Proactive APIs: refill_deck / reload_*_tipracks / refill_stacker_supply / reload_stacker_inventory.

	:param ctx: Protocol context
	:param pipette1: Primary pipette
	:param waste_bin: WasteChute or TrashBin
	:param pipette2: Optional second pipette
	:param use_gripper: Use gripper for rack moves
	:param debugging: Print debug lines to stdout
	:param suppress_comments: Suppress ctx.comment tracker messages
	'''

	# Flex rear expansion deck slot IDs; loading tipracks here requires add_expansion_slots first.
	EXPANSION_DECK_SLOTS: frozenset = frozenset({'A4', 'B4', 'C4', 'D4'})

	def __init__(self, ctx : protocol_api.ProtocolContext, pipette1 : protocol_api.InstrumentContext, 
				waste_bin : protocol_api.WasteChute | protocol_api.TrashBin, pipette2 : protocol_api.InstrumentContext | None = None,
				use_gripper : bool = False, debugging : bool = False, suppress_comments : bool = False,
				verbose_tracebacks : bool = True) -> None:
		'''Initialize tip tracking state. Prefer add_expansion_slots / add_starting_tipracks / assign_tipracks after construct.'''

		self.metadata = {
			'Author': 'Aiden McFadden',
			'Version' : '4.1.1',
			'github': 'https://github.com/Aiden133111/TipTracker',
			'README' : 'https://github.com/Aiden133111/TipTracker/blob/main/README.md'
		}

		# READ ONLY THROUGHOUT PROTOCOL, USER DOES NOT MODIFY #

		# Protocol context (read-only).
		self.ctx : protocol_api.ProtocolContext = ctx
		# Primary pipette (read-only).
		self.pipette1 : protocol_api.InstrumentContext = pipette1										
		# Optional second pipette (read-only).
		self.pipette2 : protocol_api.InstrumentContext | None = pipette2
		# Expansion slots registered via add_expansion_slots (read-only).
		self.ex_slots : list[str] = []	
		# Pick-up call counts per pipette (read-only).
		self.pick_up_count : dict[protocol_api.InstrumentContext, int] = {pipette1 : 0, pipette2 : 0}
		# Drop call counts per pipette (read-only).
		self.drop_count : dict[protocol_api.InstrumentContext, int] = {pipette1 : 0, pipette2 : 0}
		# Gripper flag from init (read-only).
		self.use_gripper : bool = use_gripper					
		# Waste chute or trash bin from init (read-only).
		self.waste : protocol_api.WasteChute | protocol_api.TrashBin = waste_bin
		# On-deck tipracks by load name (read-only).
		self.tipracks : dict[str, list[protocol_api.Labware]] = {}
		# Expansion-slot tipracks by load name (read-only).
		self.ex_racks : dict[str, list[protocol_api.Labware]] = {}
		# Vacated expansion slots by tip type (read-only).
		self.empty_ex_slots : dict[str, list[str]] = {}	
		# Refill target slots by tip type via assign_slots (read-only).
		self.rack_assignments : dict[str, list[str]] = {}	
		# Tips used per tip type (read-only).
		self.tip_counts : dict[str, int] = {}
		# Tipracks loaded per tip type (read-only).
		self.tip_rack_counts : dict[str, int] = {}
		# First open_slot value, restored after refills (read-only).
		self.original_open_slot : str | None = None	
		# Stackers by tip type via add_stacker (read-only).
		self.stackers : dict[str, list[list]] = {}
		# Pending return-to-shuttle state after stacker grab (read-only).
		self.return_to_stacker : bool = False	
		# Adapter labware by deck slot (96ch).
		self.tiprack_adapters : dict[str, list] = {}
		# Adapter pickup rack / REPLACE_ME state by tip type (read-only).
		self.adapter_pickup_tipracks : dict[str, list[protocol_api.Labware]] = {} 
		# Tip type assigned to pipette 1.
		self.pipette_1_tip_type : str | None = None
		# Tip type assigned to pipette 2.
		self.pipette_2_tip_type : str | None = None
		# Pending manual refill flag.
		self.call_refill : bool = False
		# Stackers used to store empties by tip type.
		self.storing_stackers : dict[str, list[list]] = {}
		# Last assign_tipracks layout per pipette: (mode, start, end).
		self._pipette_layouts : dict[protocol_api.InstrumentContext, tuple] = {}
		
		# READ/WRITE, USER CAN MODIFY AS NEEDED THROUGH PROTOCOL #

		# Free slot for carousel / shuttle moves (read/write).
		self.open_slot : str | None = None	
		# Debug stdout flag (read/write).
		self.debug : bool = debugging	
		# Auto-waste empties via chute when present (read/write).
		self.use_chute : bool = True if type(waste_bin) == protocol_api.WasteChute else False			 
		# Keep empties on deck and carousel instead of wasting (read/write).
		self.carousel_tips : bool = False if type(waste_bin) == protocol_api.WasteChute else True
		# Emit ctx.comment tracker messages (read/write).
		self.print_comments : bool = not suppress_comments
		# Cap racks loaded per tip type (read/write).
		self.max_racks_count : dict[str, int] = {}
		# Slots skipped for waste/refill (read/write).
		self.ignore_slots : list[str] = []
		# Forced partial-pickup slot per tip type (read/write).
		self.pick_up_slots : dict[str, str] = {}
		# Default pipette for pick_up/drop when omitted (read/write).
		self.active_pipette = None		
		# Share one adapter across tip types (read/write).
		self.global_adapter : bool = False
		# Print call stacks on fatal errors (read/write).
		self.verbose_tracebacks : bool = verbose_tracebacks

	def _tiptracker_report_error(self, headline: str, *, include_call_stack: bool | None = None) -> None:
		'''Print a visible banner and optional interpreter stack to stderr (simulation console / robot logs).'''
		if include_call_stack is None:
			include_call_stack = self.verbose_tracebacks
		print(f'\n{"=" * 60}\n[TipTracker] {headline}\n{"=" * 60}\n', file=sys.stderr)
		if include_call_stack:
			print(
				'Call stack leading to this report (find your protocol or pasted file below TipTracker entry points):\n',
				file=sys.stderr,
			)
			traceback.print_stack(limit=30, file=sys.stderr)

	def _fatal_tracker_error(self, headline: str, err: BaseException | None = None) -> None:
		'''Log traceback and optional call stack, then terminate the process (legacy behavior for configuration errors).'''
		self._tiptracker_report_error(headline, include_call_stack=self.verbose_tracebacks)
		if err is not None:
			print(f'Raised: {type(err).__name__}: {err}\n', file=sys.stderr)
		traceback.print_exc()
		exit(1)


	def _log(self, msg: str) -> None:
		"""Emit to protocol comments and/or debug stdout."""
		if self.print_comments:
			self.ctx.comment(msg)
		if self.debug:
			print(msg)

	def _deck_slot_id(self, labware_or_slot: protocol_api.Labware | str) -> str:
		"""Resolve a deck slot name (e.g. A1, B4) for comparisons to ignore_slots and ex_slots."""
		if isinstance(labware_or_slot, type(protocol_api.OFF_DECK)):
			return ""
		if isinstance(labware_or_slot, str):
			return labware_or_slot
		p = getattr(labware_or_slot, "parent", None)
		seen = 0
		while p is not None and seen < 8:
			seen += 1
			if isinstance(p, str):
				return p
			obj = getattr(p, "object", None)
			if isinstance(obj, str):
				return obj
			p = getattr(p, "parent", None)
		p0 = getattr(labware_or_slot, "parent", None)
		return str(p0) if p0 is not None else ""

	def _assignable_deck_slots(self) -> set[str]:
		"""Deck slots where tipracks may live (assignments, expansion, adapters) — not waste."""
		slots: set[str] = set()
		for slot_list in self.rack_assignments.values():
			slots.update(slot_list)
		slots.update(self.ex_slots)
		slots.update(self.tiprack_adapters.keys())
		return slots

	def _labware_is_on_deck(self, labware: protocol_api.Labware | None) -> bool:
		"""True when labware is on an assignable deck slot, not waste/trash or OFF_DECK."""
		if labware is None:
			return False
		off_deck = type(protocol_api.OFF_DECK)
		waste_chute = getattr(protocol_api, 'WASTE_CHUTE', None)
		off_deck_types = (off_deck,) + ((type(waste_chute),) if waste_chute is not None else ())
		try:
			parent = labware.parent
		except AttributeError:
			return False
		if isinstance(parent, off_deck_types):
			return False
		if self.use_chute and parent is self.waste:
			return False
		p = parent
		for _ in range(8):
			if p is None:
				break
			if isinstance(p, off_deck_types):
				return False
			if self.use_chute and p is self.waste:
				return False
			if isinstance(p, off_deck):
				return False
			p = getattr(p, 'parent', None)
		slot = self._deck_slot_id(labware)
		if not slot or slot not in self._assignable_deck_slots():
			return False
		deck_item = self.ctx.deck.get(slot)
		if deck_item is None:
			return False
		if isinstance(deck_item, protocol_api.Labware) and deck_item.load_name == 'opentrons_flex_96_tiprack_adapter':
			return deck_item.child is labware
		return deck_item is labware

	def _accessible_tipracks(self, rack_name: str) -> list:
		"""On-deck tipracks for ``rack_name`` (excludes waste chute / off-deck)."""
		return [r for r in self.tipracks.get(rack_name, []) if self._labware_is_on_deck(r)]

	def _accessible_adapter_tipracks(self, rack_name: str) -> list:
		"""On-deck adapter-mounted tipracks for ``rack_name``."""
		return [
			r for r in self.adapter_pickup_tipracks.get(rack_name, [])
			if self._labware_is_on_deck(r)
		]

	def _find_last_full_main_deck_adapter_donor(
		self, tip_load_name: str, adapter_slot: str
	) -> tuple[protocol_api.Labware | None, str | None]:
		"""Last assigned **full** main-deck tiprack of ``tip_load_name`` suitable to mount on an adapter."""
		reserved_pickup = self.pick_up_slots.get(tip_load_name)
		for slot in reversed(list(self.rack_assignments.get(tip_load_name, []))):
			if slot == adapter_slot or slot in self.tiprack_adapters:
				continue
			if slot in self.ignore_slots or slot in self.ex_slots:
				continue
			if reserved_pickup is not None and slot == reserved_pickup:
				continue
			item = self.ctx.deck.get(slot)
			if item is None or not isinstance(item, protocol_api.Labware):
				continue
			if item.load_name != tip_load_name:
				continue
			if all(well.has_tip for well in item.wells()):
				return item, slot
		return None, None

	def _empty_tiprack_labware_for_type(self, tip_load_name: str) -> list:
		"""
		Exhausted tiprack *labware* for ``tip_load_name`` on assigned slots (including adapter children).
		Never includes adapter labware objects — only the tiprack that must be wasted/replaced.
		"""
		empty: list = []
		seen: set[int] = set()

		def _add(rack) -> None:
			if rack is None or id(rack) in seen:
				return
			if not hasattr(rack, 'wells'):
				return
			if getattr(rack, 'load_name', None) == 'opentrons_flex_96_tiprack_adapter':
				return
			if not self._labware_is_on_deck(rack):
				return
			if any(well.has_tip for well in rack.wells()):
				return
			sk = self._deck_slot_id(rack)
			if sk in self.ignore_slots:
				return
			seen.add(id(rack))
			empty.append(rack)

		for slot in self._slots_for_rack_refill(tip_load_name):
			item = self.ctx.deck.get(slot)
			if item is None or not isinstance(item, protocol_api.Labware):
				continue
			if item.load_name == 'opentrons_flex_96_tiprack_adapter':
				_add(item.child)
			elif item.load_name == tip_load_name:
				_add(item)

		for rack in self.adapter_pickup_tipracks.get(tip_load_name, []):
			if isinstance(rack, str):
				continue
			_add(rack)

		for slot, datalist in self.tiprack_adapters.items():
			child = datalist[-1].child if datalist else None
			if child is not None and getattr(child, 'load_name', None) == tip_load_name:
				_add(child)

		return empty

	def _vacant_slots_for_type(self, tip_load_name: str) -> list[str]:
		"""Assigned slots with no tiprack present (bare deck or empty adapter), excluding ignore_slots."""
		vacant: list[str] = []
		for slot in self.rack_assignments.get(tip_load_name, []):
			if slot in self.ignore_slots:
				continue
			item = self.ctx.deck.get(slot)
			if item is None:
				if slot not in self.tiprack_adapters:
					vacant.append(slot)
				continue
			if not isinstance(item, protocol_api.Labware):
				continue
			if item.load_name == 'opentrons_flex_96_tiprack_adapter' and item.child is None:
				vacant.append(slot)
		for slot, datalist in self.tiprack_adapters.items():
			if tip_load_name not in self.adapter_pickup_tipracks and datalist[0] != tip_load_name:
				continue
			if datalist[-1].child is None and slot not in vacant and slot not in self.ignore_slots:
				vacant.append(slot)
		return list(dict.fromkeys(vacant))

	def _empty_tiprack_slot_ids(self, empty_tipracks: list) -> list[str]:
		"""Deck slot strings for exhausted tipracks (deduped; never adapter objects)."""
		slots: list[str] = []
		for rack in empty_tipracks:
			sk = self._deck_slot_id(rack)
			if sk and sk not in self.ignore_slots and sk not in slots:
				slots.append(sk)
		return slots

	def _pipette_nozzle_layout_params(
		self, pipette: protocol_api.InstrumentContext
	) -> tuple[NozzleConfigurationType | None, str | None, str | None]:
		"""Read ``(mode, start, end)`` from TipTracker state and/or the pipette's active layout."""
		stored = self._pipette_layouts.get(pipette)
		if stored is not None and stored[0] is not None:
			return stored  # type: ignore[return-value]

		channels = getattr(pipette, 'active_channels', None)
		active_nozzles = getattr(pipette, 'active_nozzles', None) or []
		nominal = getattr(getattr(pipette, 'config', None), 'channels', None)

		# Full head: 96 active channels, or nominal 96 with no partial nozzle set reported.
		if channels == 96 or (nominal == 96 and channels == 96):
			return ALL, None, None
		if active_nozzles:
			nozzle_list = sorted(
				active_nozzles,
				key=lambda n: (n[0], int(n[1:])),
			)
			if channels == 1 or len(nozzle_list) == 1:
				return SINGLE, nozzle_list[0], None
			rows = {n[0] for n in nozzle_list}
			cols = {int(n[1:]) for n in nozzle_list}
			if len(rows) == 1:
				start = 'A12' if 'A12' in active_nozzles else nozzle_list[0]
				return ROW, start, None
			if len(cols) == 1:
				return COLUMN, nozzle_list[0], None
			if channels and len(nozzle_list) == channels:
				return PARTIAL_COLUMN, nozzle_list[0], nozzle_list[-1]
			return None, None, None

		# Heuristic when active_nozzles is unset (common on Flex API 2.27 sim/hardware).
		if channels == 1:
			return SINGLE, 'A1', None
		if nominal == 96 and channels == 12:
			return ROW, 'A12', None
		if nominal == 96 and channels == 8:
			return COLUMN, 'A12', None
		if channels == 8 and nominal == 8:
			return COLUMN, 'A1', None
		if channels == nominal and nominal in (8, 96):
			return (ALL if nominal == 96 else None), None, None
		return None, None, None

	def _reassign_after_partial_pickup_refill(
		self,
		tip_load_name: str,
		pipette: protocol_api.InstrumentContext,
		layout: tuple[NozzleConfigurationType | None, str | None, str | None] | None = None,
	) -> None:
		"""Rebind only the forced-pickup rack and preserve the pipette's partial nozzle layout."""
		self.reset_rack_list(tip_load_name)
		forced = self._forced_pickup_labware(tip_load_name)
		if not self._labware_is_on_deck(forced):
			raise ValueError(f'No on-deck rack on forced pickup slot for {tip_load_name}')
		mode, start, end = layout or self._pipette_nozzle_layout_params(pipette)
		# Adapter ALL only when the pipette was actually in ALL (or unknown); never wipe COLUMN/ROW/SINGLE.
		if self._adapter_pickup_configured(tip_load_name) and mode in (ALL, None):
			self.assign_tipracks(tip_load_name, pipette, mode=ALL)
			return
		tip_racks = [forced]
		if mode in (COLUMN, SINGLE, ROW, PARTIAL_COLUMN):
			pipette.configure_nozzle_layout(
				style=mode, start=start, end=end, tip_racks=tip_racks,
			)
		elif mode == ALL:
			pipette.configure_nozzle_layout(style=ALL, tip_racks=tip_racks)
		else:
			pipette.tip_racks = tip_racks

	def _pipette_is_single_channel(self, pipette: protocol_api.InstrumentContext) -> bool:
		"""True for Flex/OT-2 1-channel heads (configure_nozzle_layout is not allowed)."""
		ch = getattr(getattr(pipette, 'config', None), 'channels', None)
		if ch == 1:
			return True
		text = ' '.join(
			str(x)
			for x in (getattr(pipette, 'name', None), getattr(pipette, 'model', None))
			if x is not None
		).lower().replace('_', '').replace('-', '')
		return '1channel' in text

	def _reassign_preserving_layout(
		self,
		tip_load_name: str,
		pipette: protocol_api.InstrumentContext,
		layout: tuple[NozzleConfigurationType | None, str | None, str | None] | None = None,
	) -> None:
		"""Reassign tipracks after a refill and restore the pipette's prior nozzle layout."""
		self._ensure_adapters_stocked(tip_load_name)
		self.reset_rack_list(tip_load_name)
		if self._pipette_is_single_channel(pipette):
			self.assign_tipracks(tip_load_name, pipette, mode=None)
			return
		mode, start, end = layout if layout is not None else self._pipette_nozzle_layout_params(pipette)
		if mode in (COLUMN, SINGLE, ROW, PARTIAL_COLUMN, ALL):
			self.assign_tipracks(tip_load_name, pipette, mode=mode, start=start, end=end)
		else:
			self.assign_tipracks(tip_load_name, pipette, mode=self._adapter_assign_mode(tip_load_name))

	def _slot_needs_tiprack_load(self, slot: str, tip_load_name: str | None = None) -> bool:
		"""True when ``slot`` should receive a tiprack on reload/refill."""
		if slot in self.ignore_slots:
			return False
		item = self.ctx.deck.get(slot)
		if item is None:
			return True
		adapter = None
		if slot in self.tiprack_adapters:
			adapter = self.tiprack_adapters[slot][1]
		elif getattr(item, 'load_name', None) == 'opentrons_flex_96_tiprack_adapter':
			adapter = item
		if adapter is not None:
			return adapter.child is None
		return False

	def _ensure_adapters_stocked(self, tip_load_name: str) -> None:
		"""
		If an adapter assigned for ``tip_load_name`` (or ``global_adapter``) has no child,
		shuttle a full on-deck tiprack of that type onto it so 96-channel ALL can pick up.
		"""
		adapter_slots: list[str] = []
		if self.global_adapter and self.tiprack_adapters:
			adapter_slots = list(self.tiprack_adapters.keys())
		else:
			for slot, datalist in self.tiprack_adapters.items():
				if datalist[0] == tip_load_name or slot in self.rack_assignments.get(tip_load_name, []):
					adapter_slots.append(slot)
		adapter_slots = list(dict.fromkeys(adapter_slots))

		for slot in adapter_slots:
			adapter = self.tiprack_adapters[slot][1]
			child = adapter.child
			if child is not None and any(w.has_tip for w in child.wells()):
				continue
			if child is not None and not any(w.has_tip for w in child.wells()):
				# Exhausted rack still mounted — leave for waste/refill paths
				continue

			donor, _donor_slot = self._find_last_full_main_deck_adapter_donor(tip_load_name, slot)
			if donor is None:
				reserved_pickup = self.pick_up_slots.get(tip_load_name)
				for rack in list(self.tipracks.get(tip_load_name, [])) + list(self.ex_racks.get(tip_load_name, [])):
					if not self._labware_is_on_deck(rack):
						continue
					if not all(w.has_tip for w in rack.wells()):
						continue
					sk = self._deck_slot_id(rack)
					if sk in self.tiprack_adapters:
						continue
					if reserved_pickup is not None and sk == reserved_pickup:
						continue
					donor = rack
					break
			if donor is None:
				self._log(f'No full {tip_load_name} available to mount on empty adapter {slot}')
				continue
			self._log(f'Moving {tip_load_name} onto adapter on slot {slot}')
			self._shuttle_labware(donor, adapter)
			self.tiprack_adapters[slot][0] = tip_load_name

	def _mount_tip_type_on_global_adapter(self, tip_load_name: str) -> bool:
		"""When ``global_adapter`` is True, make the shared adapter hold ``tip_load_name`` for 96-channel ALL."""
		if not self.global_adapter or not self.tiprack_adapters:
			return False
		adapter_slot = list(self.tiprack_adapters.keys())[0]
		adapter = self.tiprack_adapters[adapter_slot][1]
		current = adapter.child
		cleared_load_name = None

		if (
			current is not None
			and current.load_name == tip_load_name
			and any(w.has_tip for w in current.wells())
		):
			self.tiprack_adapters[adapter_slot][0] = tip_load_name
			if tip_load_name in self.adapter_pickup_tipracks and self.adapter_pickup_tipracks[tip_load_name] and self.adapter_pickup_tipracks[tip_load_name][0] == 'REPLACE_ME':
				self.adapter_pickup_tipracks[tip_load_name] = [current]
			return True

		if current is not None:
			cleared_load_name = current.load_name
			has_tips = any(w.has_tip for w in current.wells())
			if has_tips:
				if self.open_slot is None:
					raise ValueError(
						"global_adapter: adapter tiprack still has tips but TipTracker.open_slot is not set. "
						"Set tracker.open_slot to a free deck location to park the tipped rack before switching tip types."
					)
				dest = self.open_slot
				self._log(f'Parking tipped {cleared_load_name} from adapter {adapter_slot} to open_slot {dest}')
				self._shuttle_labware(current, dest)
			else:
				dest = self.waste if self.use_chute else (
					self.open_slot if self.open_slot is not None else protocol_api.OFF_DECK
				)
				self._log(f'Wasting empty {cleared_load_name} from adapter {adapter_slot} to {dest}')
				self._shuttle_labware(current, dest)

		# Claim adapter for the requested tip type (seeds REPLACE_ME if needed)
		self.assign_slots(tip_load_name, adapter_slot)

		replacement, source_slot = self._find_last_full_main_deck_adapter_donor(
			tip_load_name, adapter_slot
		)
		if replacement is None:
			reserved_pickup = self.pick_up_slots.get(tip_load_name)
			for rack in reversed(list(self.ex_racks.get(tip_load_name, []))):
				if not self._labware_is_on_deck(rack):
					continue
				if not all(w.has_tip for w in rack.wells()):
					continue
				sk = self._deck_slot_id(rack)
				if sk in self.tiprack_adapters:
					continue
				if reserved_pickup is not None and sk == reserved_pickup:
					continue
				replacement = rack
				source_slot = sk
				break
		if replacement is None:
			if tip_load_name in self.stackers and sum(
				stacker[1] for stacker in self.stackers.get(tip_load_name, [])
			) > 0:
				self._log(f'Grabbing {tip_load_name} from stacker onto adapter {adapter_slot}')
				self.grab_from_stacker(tip_load_name, [adapter_slot])
				self.tiprack_adapters[adapter_slot][0] = tip_load_name
				reset_names = [tip_load_name] + ([cleared_load_name] if cleared_load_name else [])
				self.reset_rack_list(reset_names)
				return adapter.child is not None and any(w.has_tip for w in adapter.child.wells())
			# Leave REPLACE_ME for pick_up / manual refill
			if tip_load_name not in self.adapter_pickup_tipracks or not self.adapter_pickup_tipracks[tip_load_name]:
				self.adapter_pickup_tipracks[tip_load_name] = ['REPLACE_ME', tip_load_name, adapter_slot]
			elif self.adapter_pickup_tipracks[tip_load_name][0] != 'REPLACE_ME':
				# Only tip racks of wrong state — force pending remount
				self.adapter_pickup_tipracks[tip_load_name] = ['REPLACE_ME', tip_load_name, adapter_slot]
			return False

		self._log(f'Moving {tip_load_name} from {source_slot} onto global adapter {adapter_slot}')
		self._shuttle_labware(replacement, adapter)
		if source_slot is not None:
			self.open_slot = source_slot
		self.tiprack_adapters[adapter_slot][0] = tip_load_name
		reset_names = [tip_load_name] + ([cleared_load_name] if cleared_load_name else [])
		self.reset_rack_list(reset_names)
		return True

	def _pipette_is_flex_96channel(self, pipette: protocol_api.InstrumentContext) -> bool:
		'''True for Flex 96-channel heads even when ``pipette.config.channels`` is unset (some sim contexts).'''
		ch = getattr(getattr(pipette, 'config', None), 'channels', 0)
		if ch == 96:
			return True
		text = ' '.join(
			str(x)
			for x in (
				getattr(pipette, 'name', None),
				getattr(pipette, 'model', None),
				getattr(getattr(pipette, 'model_metadata', None), 'display_name', None),
			)
			if x is not None
		).lower()
		return '96' in text and 'channel' in text

	def _adapter_pickup_configured(self, tip_load_name: str) -> bool:
		"""True when this tip type is set up for adapter pickup (real rack on adapter or pending REPLACE_ME shuffle)."""
		ap = self.adapter_pickup_tipracks.get(tip_load_name)
		if not ap:
			return False
		if ap[0] == 'REPLACE_ME':
			return True
		return any(hasattr(r, 'wells') for r in ap)

	def _shuttle_target_for_stacker_place(
		self, location: str | protocol_api.Labware | protocol_api.ModuleContext
	) -> str | protocol_api.Labware | protocol_api.ModuleContext:
		'''
		Destination for ``move_labware`` when placing a full tiprack from a stacker onto the deck.
		If ``location`` is a slot id that has a Flex tiprack adapter, return that adapter labware so the new rack mounts on the adapter (avoids LocationIsOccupied on the slot).
		'''
		if isinstance(location, str) and location in self.tiprack_adapters:
			return self.tiprack_adapters[location][1]
		return location

	def _waste_empty_rack_now(self, rack: protocol_api.Labware, tip_load_name: str) -> bool:
		"""
		Whether an empty rack should go to waste before expansion/stacker refill handling.
		Skip: racks on ignore_slots (reuse); empties on expansion slots while ex_racks still has racks to shuttle in.
		"""
		sk = self._deck_slot_id(rack)
		if sk in self.ignore_slots:
			return False
		if sk in self.ex_slots and self.ex_racks.get(tip_load_name):
			return False
		return True

	def _slot_has_empty_tiprack_of_type(self, deck_slot: str, tip_load_name: str) -> bool:
		"""True if this slot holds an exhausted tiprack of the given API load name (ready to clear)."""
		deck_item = self.ctx.deck[deck_slot]
		if deck_item is None:
			return False
		if deck_item.load_name == 'opentrons_flex_96_tiprack_adapter':
			child_rack = deck_item.child
			if child_rack is None or child_rack.load_name != tip_load_name:
				return False
			return not any(w.has_tip for w in child_rack.wells())
		if deck_item.load_name != tip_load_name:
			return False
		return not any(w.has_tip for w in deck_item.wells())

	def _planned_refill_load_slots(
		self, tip_load_name: str, slot_list: list[str], *, waste_all_old: bool = True
	) -> list[str]:
		"""Ordered unique slots ``refill_tips`` passes to ``load_tipracks`` (cleared empties + vacant targets from ``slot_list``)."""
		if waste_all_old:
			candidate_slots = [s for s in self.rack_assignments.get(tip_load_name, []) if s not in self.ignore_slots]
		else:
			candidate_slots = [s for s in slot_list if s not in self.ignore_slots]
		clear_slots = [s for s in candidate_slots if self._slot_has_empty_tiprack_of_type(s, tip_load_name)]
		return list(dict.fromkeys(clear_slots + [s for s in slot_list if self.ctx.deck[s] is None]))

	def _cap_slots_to_max_rack_budget(
		self, tip_load_name: str, ordered_slots: list[str], *, count_before: int | None = None
	) -> list[str]:
		"""
		Prefix of ``ordered_slots`` that may still receive a rack under ``max_racks_count``,
		mirroring ``load_tipracks`` (one rack budget consumed per listed slot in order).
		"""
		max_c = self.max_racks_count.get(tip_load_name)
		if max_c is None:
			return list(ordered_slots)
		sim = self.tip_rack_counts.get(tip_load_name, 0) if count_before is None else count_before
		out: list[str] = []
		for s in ordered_slots:
			if sim >= max_c:
				break
			out.append(s)
			sim += 1
		return out

	# ------------------------------------------------------------------ #
	# V2 supply / refill inventory (public dicts unchanged for callers)   #
	# ------------------------------------------------------------------ #

	def _slot_region(self, slot_id: str) -> DeckRegion:
		if slot_id in self.tiprack_adapters:
			return DeckRegion.ADAPTER
		if slot_id in self.ex_slots:
			return DeckRegion.EXPANSION
		return DeckRegion.MAIN

	def _stacker_supplies(self, tip_load_name: str) -> list[StackerSupply]:
		return [StackerSupply.from_legacy_row(row) for row in self.stackers.get(tip_load_name, [])]

	def _write_stacker_supplies(self, tip_load_name: str, supplies: list[StackerSupply]) -> None:
		self.stackers[tip_load_name] = [s.to_legacy_row() for s in supplies]

	def _stacker_total_count(self, tip_load_name: str) -> int:
		return sum(s.rack_count for s in self._stacker_supplies(tip_load_name))

	def _expansion_supply_racks(self, tip_load_name: str) -> list[protocol_api.Labware]:
		return list(self.ex_racks.get(tip_load_name, []))

	def _main_deck_supply_racks(self, tip_load_name: str) -> list[protocol_api.Labware]:
		return list(self.tipracks.get(tip_load_name, []))

	def _has_registered_external_supply(self, tip_load_name: str) -> bool:
		'''True when expansion staging or stackers were configured for this tip type.'''
		return tip_load_name in self.ex_racks or tip_load_name in self.stackers

	def _expansion_supply_available(self, tip_load_name: str) -> bool:
		return bool(self._expansion_supply_racks(tip_load_name))

	def _stacker_supply_available(self, tip_load_name: str) -> bool:
		return self._stacker_total_count(tip_load_name) > 0

	def _refill_deposit_slots(
		self,
		tip_load_name: str,
		empty_tiprack_slots: list,
		vacant_slots: list[str],
	) -> list[str]:
		'''Resolve main-deck targets when shuttling a full rack from expansion or stacker.'''
		deposit = [
			slot_id for slot_id in (
				self._deck_slot_id(s) or (s if isinstance(s, str) else '') for s in empty_tiprack_slots
			)
			if slot_id and slot_id not in self.ignore_slots
		]
		if not deposit:
			deposit = [s for s in vacant_slots if s not in self.ignore_slots]
		if self._adapter_pickup_configured(tip_load_name):
			adapter_slots = [s for s in deposit if s in self.tiprack_adapters]
			other_slots = [s for s in deposit if s not in self.tiprack_adapters]
			for slot in vacant_slots:
				if slot in self.tiprack_adapters and slot not in adapter_slots:
					adapter_slots.append(slot)
			deposit = adapter_slots + other_slots
		return list(dict.fromkeys(deposit))

	def _adapter_assign_mode(self, tip_load_name: str):
		'''Nozzle mode for pipette reassignment after a refill when adapter pickup is active.'''
		return ALL if self._adapter_pickup_configured(tip_load_name) else None

	def _collect_empty_main_deck_state(
		self, tip_load_name: str, slots_to_check: list[str]
	) -> tuple[list, list[str], list[str]]:
		"""Return ``(empty_tipracks, vacant_slots, empty_tiprack_slot_ids)`` for the main grid."""
		empty_tipracks = self._empty_tiprack_labware_for_type(tip_load_name)
		vacant_slots = self._vacant_slots_for_type(tip_load_name)
		empty_tiprack_slots = self._empty_tiprack_slot_ids(empty_tipracks)
		return empty_tipracks, vacant_slots, empty_tiprack_slots

	def _build_refill_snapshot(
		self,
		tip_load_name: str,
		*,
		refill_all: bool,
		waste_empties: bool = True,
	) -> RefillSnapshot:
		slots_to_check = self._slots_for_rack_refill(tip_load_name)
		empty_tipracks, vacant_slots, empty_tiprack_slots = self._collect_empty_main_deck_state(
			tip_load_name, slots_to_check
		)
		deposit_slots = self._refill_deposit_slots(tip_load_name, empty_tiprack_slots, vacant_slots)
		stacker_carousel_olds = [
			r for r in empty_tipracks if self._deck_slot_id(r) not in self.ex_slots
		]
		wasted_slot_ids: list[str] = []
		if waste_empties and not self.carousel_tips:
			empties_to_waste = [r for r in empty_tipracks if self._waste_empty_rack_now(r, tip_load_name)]
			wasted_slot_ids = [self._deck_slot_id(r) for r in empties_to_waste if self._deck_slot_id(r)]
			self.waste_tips(empties_to_waste)
			if wasted_slot_ids:
				deposit_slots = list(dict.fromkeys(wasted_slot_ids + deposit_slots))
		snap = RefillSnapshot(
			tip_load_name=tip_load_name,
			slots_to_check=slots_to_check,
			vacant_slots=vacant_slots,
			empty_tipracks=empty_tipracks,
			empty_tiprack_slots=empty_tiprack_slots,
			deposit_slots=deposit_slots,
			wasted_slot_ids=wasted_slot_ids,
			stacker_carousel_olds=stacker_carousel_olds,
		)
		if refill_all:
			snap.other_rack_slots = {}
			for rack_load_name, rack_list in self.tipracks.items():
				if rack_load_name == tip_load_name:
					continue
				empties = []
				for rack in rack_list:
					if any(well.has_tip for well in rack.wells()):
						continue
					sk = self._deck_slot_id(rack)
					if sk in self.ignore_slots:
						continue
					if sk in self.ex_slots and self._expansion_supply_racks(rack_load_name):
						continue
					rp = rack.parent
					if isinstance(rp, type(protocol_api.OFF_DECK)):
						continue
					empties.append(rp)
				if empties:
					snap.other_rack_slots[rack_load_name] = empties
			snap.empty_tip_slots = {
				rl: [
					slot for slot in racklist
					if slot not in self.ignore_slots and self.ctx.deck[slot] is None
				]
				for rl, racklist in self.rack_assignments.items()
			}
		return snap

	def _refill_other_types_if_requested(self, snap: RefillSnapshot, *, refill_all: bool) -> None:
		if not refill_all:
			return
		self._log('Refilling all other tips')
		for other_name, other_slots in snap.other_rack_slots.items():
			if other_slots or snap.empty_tip_slots.get(other_name):
				if other_slots:
					self.waste_tips(other_slots)
				self.refill_deck(
					other_name,
					slots=other_slots + snap.empty_tip_slots.get(other_name, []),
					reassign_pipette=False,
				)

	def _reassign_after_refill(
		self,
		tip_load_name: str,
		pipette: protocol_api.InstrumentContext,
		layout: tuple[NozzleConfigurationType | None, str | None, str | None] | None = None,
	) -> None:
		"""Reset tracking and restore prior nozzle layout (V1 parity + 1ch-safe)."""
		self._reassign_preserving_layout(tip_load_name, pipette, layout)

	def _refill_from_expansion_supply(
		self,
		snap: RefillSnapshot,
		pipette: protocol_api.InstrumentContext,
		locus,
		*,
		pickup: bool = True,
	) -> int:
		self._log('Tiprack on expansion slot, moving to active deck')
		return_code = 2
		moved_from_expansion = False
		if self.carousel_tips:
			# tipracks[] / main-deck supply often empty for expansion-only / global-adapter layouts;
			# pair expansion supply against exhausted adapter/main-deck racks instead.
			old_racks = list(self._main_deck_supply_racks(snap.tip_load_name))
			if not old_racks:
				old_racks = [
					r for r in snap.empty_tipracks
					if self._deck_slot_id(r) not in self.ex_slots
				]
			for old_rack, e_rack in zip(old_racks, self._expansion_supply_racks(snap.tip_load_name)):
				self.carousel(old_rack, e_rack)
				moved_from_expansion = True
				return_code = 1
		if not self.carousel_tips or not moved_from_expansion:
			for e_rack, target_slot in zip(self._expansion_supply_racks(snap.tip_load_name), snap.deposit_slots):
				e_slot_source = e_rack.parent
				self._shuttle_labware(e_rack, self._shuttle_target_for_stacker_place(target_slot))
				if snap.tip_load_name in self.empty_ex_slots:
					self.empty_ex_slots[snap.tip_load_name].append(e_slot_source)
				else:
					self.empty_ex_slots[snap.tip_load_name] = [e_slot_source]
				return_code = 2
				self.open_slot = self._deck_slot_id(e_slot_source) or e_slot_source
		self._reassign_after_refill(snap.tip_load_name, pipette, getattr(snap, 'layout', None))
		if pickup:
			pipette.pick_up_tip(locus)
		return return_code

	def _refill_from_stacker_supply(
		self,
		snap: RefillSnapshot,
		pipette: protocol_api.InstrumentContext,
		locus,
		*,
		pickup: bool = True,
	) -> int:
		if self.carousel_tips:
			self.grab_from_stacker(snap.tip_load_name, snap.stacker_carousel_olds)
		else:
			stacker_targets = list(dict.fromkeys(
				[s for s in snap.deposit_slots + snap.wasted_slot_ids if s not in self.ignore_slots]
			))
			self.grab_from_stacker(snap.tip_load_name, stacker_targets)
		self._reassign_after_refill(snap.tip_load_name, pipette, getattr(snap, 'layout', None))
		if pickup:
			pipette.pick_up_tip(locus)
		return 3

	def _refill_manually_when_supply_exhausted(
		self,
		snap: RefillSnapshot,
		pipette: protocol_api.InstrumentContext,
		locus,
		*,
		pickup: bool = True,
	) -> int:
		self.call_refill = True
		if snap.tip_load_name in self.stackers:
			self.refill_stacker_supply(
				snap.tip_load_name,
				deposit_targets=snap.empty_tipracks,
			)
		if (
			not set(self.EXPANSION_DECK_SLOTS).isdisjoint(self.rack_assignments.get(snap.tip_load_name, []))
			and self.call_refill
		):
			self._log('No remaining tipracks on expansion deck, manual refill needed')
		if self.call_refill:
			self.reload_deck_tipracks(snap.tip_load_name, self.rack_assignments[snap.tip_load_name])
		self._reassign_after_refill(snap.tip_load_name, pipette, getattr(snap, 'layout', None))
		self.open_slot = self.original_open_slot
		if pickup:
			pipette.pick_up_tip(locus)
		return 4

	def _handle_main_deck_exhausted(
		self,
		tip_load_name: str,
		pipette: protocol_api.InstrumentContext,
		snap: RefillSnapshot,
		locus,
		*,
		refill_all: bool,
		pickup: bool = True,
	) -> int:
		'''Refill priority when the main deck has no tips:'''
		if not self._has_registered_external_supply(tip_load_name):
			self._log('No expansion slots / stackers defined, Refilling Manually')
			self.refill_deck(tip_load_name, pipette, snap.slots_to_check)
			self._reassign_after_refill(tip_load_name, pipette, getattr(snap, 'layout', None))
			self._refill_other_types_if_requested(snap, refill_all=refill_all)
			if pickup:
				pipette.pick_up_tip(locus)
			return 4
		self._log('Expansion slots or stackers defined, starting refilling process')
		self._refill_other_types_if_requested(snap, refill_all=refill_all)
		if self._expansion_supply_available(tip_load_name):
			return self._refill_from_expansion_supply(snap, pipette, locus, pickup=pickup)
		if self._stacker_supply_available(tip_load_name):
			return self._refill_from_stacker_supply(snap, pipette, locus, pickup=pickup)
		return self._refill_manually_when_supply_exhausted(snap, pipette, locus, pickup=pickup)

	def assign_slots(self, tiprack1 : str, slots1 : str | list[str], tiprack2 : str = None, slots2 : list[str] | str = None,
				   tiprack3 : str = None, slots3 : str | list[str] = None, tiprack4 : str | None = None, slots4 : str | list[str] | None = None,
				   clear_other_slots: bool = False) -> None:
		'''
		Assign slot(s) to a tiprack type. These are the slots that tipracks should be replaced into and does not need to be the places they are currently in or \
		where they are originally loaded in with add_starting_tipracks(). This function can be called as needed to change where racks should be loaded in any \
		given parts of the protocol, but is generally not updated much within a normal protocol deck map. Arguments must be passed as tiprack-slot pairs. 
		
		:param self: TipTracker object
		:param tiprack1: The API load name of the first tiprack type to assign slots to, for example 'opentrons_flex_96_tiprack_50ul'
		:type tiprack1: str
		:param slots1: The slot(s) to assign to tiprack1, these are the slots that tipracks of this type will be reloaded onto when they are empty, can be a string of a single slot or a list of strings for multiple slots
		:type slots1: str | list[str]
		:param tiprack2: The API load name of the second tiprack type to assign slots to, for example 'opentrons_flex_96_tiprack_50ul'
		:type tiprack2: str
		:param slots2: The slot(s) to assign to tiprack2, these are the slots that tipracks of this type will be reloaded onto when they are empty, can be a string of a single slot or a list of strings for multiple slots
		:type slots2: list[str] | str
		:param tiprack3: The API load name of the third tiprack type to assign slots to, for example 'opentrons_flex_96_tiprack_50ul'
		:type tiprack3: str
		:param slots3: The slot(s) to assign to tiprack3, these are the slots that tipracks of this type will be reloaded onto when they are empty, can be a string of a single slot or a list of strings for multiple slots
		:type slots3: str | list[str]
		:param tiprack4: The API load name of the fourth tiprack type to assign slots to, for example 'opentrons_flex_96_tiprack_50ul'
		:type tiprack4: str | None
		:param slots4: The slot(s) to assign to tiprack4, these are the slots that tipracks of this type will be reloaded onto when they are empty, can be a string of a single slot or a list of strings for multiple slots
		:type slots4: str | list[str] | None
		:param clear_other_slots: Whether to clear other slot assignments for the same tiprack type. Default behavior is to append slots to existing list
		:return: None
		:rtype: None
		'''
		all_slots = [slots1, slots2, slots3, slots4]
		all_tip_load_names = [tiprack1, tiprack2, tiprack3, tiprack4]
		for x, slot in enumerate(all_slots):
			if type(slot) != list:
				all_slots[x] = [slot]
		try:
			if len(set([t for t in all_tip_load_names if t != None])) != len([t for t in all_tip_load_names if t != None]):
				raise ValueError("Duplicate tiprack types detected, please ensure all tiprack slots are added under one tiprack argument")
			if len(set([slot for slot_list in all_slots for slot in slot_list if slot != None])) != len([slot for slot_list in all_slots for slot in slot_list if slot != None]):
				raise ValueError("Duplicate slots detected, please ensure all slots are unique across tiprack arguments")
		except ValueError as Error:
			self._fatal_tracker_error('assign_slots: structure validation failed (duplicate tiprack types or duplicate slots)', Error)
		for tip_load_name, assigned_slots in zip(all_tip_load_names, all_slots):
			try:
				if tip_load_name == None and assigned_slots == [None]:
					continue
				if tip_load_name == None and assigned_slots != [None]:
					raise ValueError("Slots provided without a corresponding tiprack")
				if tip_load_name != None and assigned_slots == [None]:
					raise ValueError("Tiprack provided without a corresponding slot or slots")
			except ValueError as Error:
				self._fatal_tracker_error('assign_slots: invalid tiprack/slot pairing in assign_slots()', Error)
			for other_tip_load_name, other_assigned_slots in self.rack_assignments.items():
				if other_tip_load_name != tip_load_name:
					if any(deck_slot in other_assigned_slots for deck_slot in assigned_slots):
						print(f'Slot conflict detected for {tip_load_name} in slots {assigned_slots} with {other_tip_load_name} in slots {other_assigned_slots}')
						self.rack_assignments[other_tip_load_name] = [deck_slot for deck_slot in other_assigned_slots if deck_slot not in assigned_slots]
			for deck_slot in assigned_slots:
				if deck_slot in self.tiprack_adapters.keys():
					self._log(f'Overwriting adapter on slot {deck_slot} from {self.tiprack_adapters[deck_slot][0]} to {tip_load_name}')
					self.tiprack_adapters[deck_slot][0] = tip_load_name
					if tip_load_name not in self.adapter_pickup_tipracks.keys():
						self.adapter_pickup_tipracks[tip_load_name] = [f'REPLACE_ME',tip_load_name,deck_slot]
			if clear_other_slots or tip_load_name not in self.rack_assignments.keys():
				self.rack_assignments[tip_load_name] = list(assigned_slots)
			else:
				self.rack_assignments[tip_load_name] = list(dict.fromkeys(
					self.rack_assignments[tip_load_name] + assigned_slots
				))
		


	def load_tipracks(self, tiprack1 : str, slots1 : str | list[str], tiprack2 : str = None,slots2 : list[str] | str = None,
				   tiprack3 : str = None, slots3 : str | list[str] = None,tiprack4 : str | None = None, slots4 : str | list[str] | None = None,
				   adapters : list[str] = []) -> None:
		'''
		Load tipracks to the deck and to the internal data. This method is automatically called when using add_starting_tipracks() and when refilling tips so \
		this is only needed to use if you want to override that completely and load new tipracks into other slots independently. Note that this \
		does not pause the protocol and is used to replace ProtocolContext.load_labware(), so do not call this function unless inteneded to not pause \
		Use starting tipracks to intially load the deck to also assign the same slots to the tipracks instead of calling this directly at the start of the protocol. \
		Can take four tipracks-slot pairs at once. If the max_racks for that given rack type has been defined and reached, it will not load any more
		
		:param self: TipTracker object
		:param tiprack1: API load name for the first tiprack type
		:type tiprack1: str
		:param slots1: Slot or list of slots for tiprack1
		:type slots1: str | list[str]
		:param tiprack2: API load name for the second tiprack type, if any
		:type tiprack2: str
		:param slots2: Slot or list of slots for tiprack2
		:type slots2: list[str] | str
		:param tiprack3: API load name for the third tiprack type, if any
		:type tiprack3: str
		:param slots3: Slot or list of slots for tiprack3
		:type slots3: str | list[str]
		:param tiprack4: API load name for the fourth tiprack type, if any
		:type tiprack4: str | None
		:param slots4: Slot or list of slots for tiprack4
		:type slots4: str | list[str] | None
		:param adapters: List of slots that should have adapters, if any. 
		:raises ValueError: If a Flex expansion deck slot (A4–D4) is used without add_expansion_slots for it first.
		:return: None
		:rtype: None
		'''
		def _slots_as_list(slots):
			if slots is None:
				return [None]
			return [slots] if isinstance(slots, str) else slots

		slots1 = _slots_as_list(slots1)
		slots2 = _slots_as_list(slots2)
		slots3 = _slots_as_list(slots3)
		slots4 = _slots_as_list(slots4)

		exp_in_this_load = sorted(
			{s for group in (slots1, slots2, slots3, slots4) for s in group if isinstance(s, str) and s in self.EXPANSION_DECK_SLOTS}
		)
		if exp_in_this_load != [] and self.ex_slots == []:
			raise ValueError("Expansion slots are not registered, please call add_expansion_slots() before loading tipracks")

		#Load labware for each tiprack in each slot
		for tip_load_name, slot_group in zip([tiprack1, tiprack2, tiprack3, tiprack4],[slots1, slots2, slots3, slots4]):
			if tip_load_name != None:
				for slot in slot_group:
					if self.max_racks_count.get(tip_load_name,None) != None:
						if self.max_racks_count[tip_load_name] == self.tip_rack_counts.get(tip_load_name,0):
							self._log(f'Max racks of {tip_load_name} reached, not loading more')
							continue
					if type(slot) == str:
						if slot in adapters or slot in self.tiprack_adapters.keys():
							if self.tiprack_adapters.get(slot,None) == None:
								self._log(f'Loading adapter for {tip_load_name} in slot {slot}')
								adapter = self.ctx.load_adapter('opentrons_flex_96_tiprack_adapter',slot)
								rack = adapter.load_labware(tip_load_name)
								self.tiprack_adapters[slot] = [tip_load_name, adapter]
							else:
								self._log(f'Adapter already on slot {slot}, loading {tip_load_name} onto adapter')
								adapter = self.tiprack_adapters[slot][1]
								existing = adapter.child
								if existing is not None and getattr(existing, 'load_name', None) == tip_load_name:
									rack = existing
									self.tiprack_adapters[slot] = [tip_load_name, adapter]
									self._log(f'Adapter on {slot} already holds {tip_load_name}; skipping duplicate load_labware')
								else:
									if existing is not None:
										self.ctx.move_labware(
											existing,
											self.waste if self.use_chute else protocol_api.OFF_DECK,
											self.use_gripper,
										)
									rack = adapter.load_labware(tip_load_name)
									self.tiprack_adapters[slot] = [tip_load_name, adapter]
							if rack not in self.adapter_pickup_tipracks.get(tip_load_name, []):
								if tip_load_name in self.adapter_pickup_tipracks.keys():
									self.adapter_pickup_tipracks[tip_load_name].append(rack)
								else:
									self.adapter_pickup_tipracks[tip_load_name] = [rack]
								if tip_load_name not in self.tip_rack_counts.keys():
									self.tip_rack_counts[tip_load_name] = 1
								else:
									self.tip_rack_counts[tip_load_name] = self.tip_rack_counts[tip_load_name] + 1
							if self.ex_slots != None and slot in self.ex_slots:
								if rack not in self.ex_racks.get(tip_load_name, []):
									if tip_load_name in self.ex_racks.keys():
										self.ex_racks[tip_load_name].append(rack)
									else:
										self.ex_racks[tip_load_name] = [rack]
							continue

						deck_here = self.ctx.deck[slot]
						if deck_here is not None and getattr(deck_here, 'load_name', None) == tip_load_name:
							rack = deck_here
							self._log(f'Slot {slot} already has {tip_load_name}; skipping load_labware')
						else:
							rack = self.ctx.load_labware(tip_load_name, slot)
						if tip_load_name not in self.tip_rack_counts.keys():
							self.tip_rack_counts[tip_load_name] = 1
						else:
							self.tip_rack_counts[tip_load_name] = self.tip_rack_counts[tip_load_name] + 1
					elif type(slot) == protocol_api.Labware and slot.load_name == 'opentrons_flex_96_tiprack_adapter':
						rack = slot.load_labware(tip_load_name)
						self.tiprack_adapters[slot.parent][0] = tip_load_name
						self._log(f'Loading {tip_load_name} onto adapter in slot {slot.parent}')
						if tip_load_name in self.adapter_pickup_tipracks.keys():
							self.adapter_pickup_tipracks[tip_load_name].append(rack)
						else:
							self.adapter_pickup_tipracks[tip_load_name] = [rack]
					elif type(slot) == protocol_api.Labware and slot.load_name == tip_load_name:
						rack = slot
						if tip_load_name not in self.tip_rack_counts.keys():
							self.tip_rack_counts[tip_load_name] = 1
						else:
							self.tip_rack_counts[tip_load_name] = self.tip_rack_counts[tip_load_name] + 1
						if tip_load_name in self.tipracks.keys():
							self.tipracks[tip_load_name].append(rack)
						else:
							self.tipracks[tip_load_name] = [rack]
					if self.ex_slots != None and type(slot) == str and slot in self.ex_slots:
						if rack not in self.ex_racks.get(tip_load_name, []):
							if tip_load_name in self.ex_racks.keys():
								self.ex_racks[tip_load_name].append(rack)
							else:
								self.ex_racks[tip_load_name] = [rack]
					elif type(slot) == str and slot in self.tiprack_adapters.keys():
						if rack not in self.adapter_pickup_tipracks.get(tip_load_name, []):
							if tip_load_name in self.adapter_pickup_tipracks.keys():
								self.adapter_pickup_tipracks[tip_load_name].append(rack)
							else:
								self.adapter_pickup_tipracks[tip_load_name] = [rack]
					else:
						if rack not in self.tipracks.get(tip_load_name, []):
							if tip_load_name in self.tipracks.keys():
								self.tipracks[tip_load_name].append(rack)
							else:
								self.tipracks[tip_load_name] = [rack]


	def _prepare_adapter_for_pickup(
		self,
		active_pipette: protocol_api.InstrumentContext,
		tip_load_name: str,
		vacant_slots: list[str],
		empty_tiprack_slots: list[str],
	) -> None:
		"""Mount the requested tip type on the global/REPLACE_ME adapter before ALL pickup."""
		# global_adapter / REPLACE_ME: clear wrong tip type from adapter, park tipped racks on open_slot
		# (or waste empties), then mount the requested tip type before 96-channel ALL pickup.
		_replace_me = self.adapter_pickup_tipracks.get(tip_load_name, [])
		_adapter_child = None
		_adapter_slot = None
		if self.tiprack_adapters:
			_adapter_slot = list(self.tiprack_adapters.keys())[0]
			_adapter_child = self.tiprack_adapters[_adapter_slot][1].child
		# Partial layouts (COLUMN/ROW/SINGLE/PARTIAL_COLUMN) pick up from main-deck racks, not the
		# adapter, so never swap the adapter or force ALL out from under them.
		_layout_before_adapter_swap = self._pipette_nozzle_layout_params(active_pipette)
		_layout_is_all = _layout_before_adapter_swap[0] in (ALL, None)
		_needs_adapter_swap = self._pipette_is_flex_96channel(active_pipette) and _layout_is_all and (
			(len(active_pipette.tip_racks) >= 3 and active_pipette.tip_racks[0] == 'REPLACE_ME')
			or (_replace_me and _replace_me[0] == 'REPLACE_ME')
			or (
				self.global_adapter
				and _adapter_slot is not None
				and (
					_adapter_child is None
					or _adapter_child.load_name != tip_load_name
					or not any(w.has_tip for w in _adapter_child.wells())
				)
			)
		)
		if _needs_adapter_swap and self.global_adapter and self.tiprack_adapters:
			if not self._mount_tip_type_on_global_adapter(tip_load_name):
				self._log(f'No full {tip_load_name} available for global adapter; starting manual refill')
				self._refill_deck_manually([_adapter_slot], tip_load_name)
				self._mount_tip_type_on_global_adapter(tip_load_name)
			self.assign_tipracks(tip_load_name, active_pipette, mode=ALL)
		elif _layout_is_all and (
			(len(active_pipette.tip_racks) >= 3 and active_pipette.tip_racks[0] == 'REPLACE_ME')
			or (_replace_me and _replace_me[0] == 'REPLACE_ME' and self._pipette_is_flex_96channel(active_pipette))
		):
			self._log('Adapter pickup tiprack empty, finding replacement')
			replacement_rack = None
			if len(active_pipette.tip_racks) >= 3 and active_pipette.tip_racks[0] == 'REPLACE_ME':
				replacement_rack_name = active_pipette.tip_racks[1]
				adapter_slot = active_pipette.tip_racks[2]
			else:
				replacement_rack_name = _replace_me[1]
				adapter_slot = _replace_me[2]
			adapter = self.tiprack_adapters[adapter_slot][1]
			current_child = adapter.child
			trash_rack_name = current_child.load_name if current_child is not None else None
			if current_child is not None:
				has_tips = any(w.has_tip for w in current_child.wells())
				if has_tips:
					if self.open_slot is None:
						raise ValueError(
							"No open slot defined; set TipTracker.open_slot to park a tipped rack cleared from the adapter"
						)
					clear_dest = self.open_slot
				else:
					clear_dest = self.waste if self.use_chute else (
						self.open_slot if self.open_slot is not None else protocol_api.OFF_DECK
					)
				self._log(f'Moving tiprack on adapter on slot {adapter_slot} to {clear_dest} to free up slot for replacement')
				self.ctx.move_labware(current_child, clear_dest, self.use_gripper)
			replacement_rack, source_slot = self._find_last_full_main_deck_adapter_donor(
				replacement_rack_name, adapter_slot
			)
			if replacement_rack is not None:
				self._log(f'Found replacement rack {replacement_rack_name} for adapter on slot {source_slot}')
				self.ctx.move_labware(replacement_rack, adapter, self.use_gripper)
				self.open_slot = source_slot
			if replacement_rack is None:
				reserved_pickup = self.pick_up_slots.get(replacement_rack_name)
				for rack in reversed(list(self.ex_racks.get(replacement_rack_name, []))):
					if not self._labware_is_on_deck(rack):
						continue
					if not all(well.has_tip for well in rack.wells()):
						continue
					source_slot = self._deck_slot_id(rack)
					if source_slot in self.tiprack_adapters:
						continue
					if reserved_pickup is not None and source_slot == reserved_pickup:
						continue
					self._log(f'Found replacement rack {replacement_rack_name} on expansion, moving to adapter')
					self._shuttle_labware(rack, adapter)
					replacement_rack = rack
					if source_slot:
						self.open_slot = source_slot
					break
			if replacement_rack == None:
				if replacement_rack_name in self.stackers.keys() and sum([stacker[1] for stacker in self.stackers.get(replacement_rack_name, [])]) > 0:
					self._log(f'Grabbing {replacement_rack_name} from stacker')
					self.grab_from_stacker(replacement_rack_name, vacant_slots + empty_tiprack_slots)
				else:
					self._log(f'No full racks available for {replacement_rack_name} on adapter, starting manual refill')
					self._refill_deck_manually([adapter_slot], replacement_rack_name)
			self.reset_rack_list([replacement_rack_name] + ([trash_rack_name] if trash_rack_name else []))
			self._reassign_preserving_layout(
				replacement_rack_name, active_pipette, _layout_before_adapter_swap
			)

	def pick_up(self, pipette : int | str | protocol_api.InstrumentContext | None = None, 
			 locus : protocol_api.Labware | protocol_api.Well | None = None, refill_all : bool = False, set_active_pipette : bool = False) -> int:
		'''Replace InstrumentContext.pick_up_tip with tracked refill (expansion → stacker → manual).'''
		#Set original open slot the first time after open_slot is defined
		if self.open_slot != None and self.original_open_slot == None:
			self.original_open_slot = self.open_slot

		#Assign proper pipette, check current tip and handle errors#
		active_pipette = None
		if pipette != None:
			active_pipette = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
			if set_active_pipette:
				self.active_pipette = active_pipette
		elif self.active_pipette != None:
			active_pipette = self.active_pipette
		try:
			if active_pipette is None and pipette in (2, '2', 'two', 'Two') and self.pipette2 is None:
				raise ValueError(
					"Requested pipette 2 but TipTracker has no second pipette (pipette2=None). "
					"Use 1, omit the pipette argument, or pass the pipette object."
				)
			if active_pipette == None:
				raise ValueError(f"Invalid pipette: {pipette}, must be in [1,'1',self.pipette1,'one','One'] or [2,'2',self.pipette2,'two','Two']")
			if pipette == None and self.active_pipette == None:
				raise ValueError(f"Active pipette not set but no pipette argument was passed, please set active pipette or specify pipette in call")
			tip_load_name = self.pipette_1_tip_type if active_pipette == self.pipette1 else self.pipette_2_tip_type
			if tip_load_name is None:
				raise ValueError(f"No tipracks assigned to pipette {active_pipette}, please assign tipracks before picking up tips")
		except ValueError as Error:
			self._fatal_tracker_error('pick_up: invalid pipette argument, missing active pipette, or no tip type assigned', Error)
		# V2: snapshot main-deck exhaustion before pickup / adapter shuffle #
		slots_to_check = self._slots_for_rack_refill(tip_load_name)
		empty_tipracks, vacant_slots, empty_tiprack_slots = self._collect_empty_main_deck_state(
			tip_load_name, slots_to_check
		)
		self._prepare_adapter_for_pickup(
			active_pipette, tip_load_name, vacant_slots, empty_tiprack_slots
		)
		#Try and pick up tip
		
		#If this rack should only be on the slot
		if tip_load_name in self.pick_up_slots.keys():
			#Check if tiprack has tips first
			next_tip = self.ctx.deck[self.pick_up_slots[tip_load_name]].next_tip()
			if next_tip == None:
				self._log(f'No tips available for pickup on slot {self.pick_up_slots[tip_load_name]}, shuffling tipracks')
				if tip_load_name in self.pick_up_slots:
					self.shuffle_for_forced_pickup(
						tip_load_name,
						self.pick_up_slots[tip_load_name],
						active_pipette,
						layout=self._pipette_nozzle_layout_params(active_pipette),
					)
		try:
			if (
				self._pipette_is_flex_96channel(active_pipette)
				and not self._adapter_pickup_configured(tip_load_name)
				and active_pipette.active_channels == 96
			):
				raise ValueError(
					f'No adapter pickup tiprack defined for {tip_load_name} but pipette has 96 active channels; '
					'use assign_tipracks with adapter pickup, deck ALL layout, or an 8-channel pipette for this tip type.'
				)
		except ValueError as Error:
			self._fatal_tracker_error('pick_up: 96-channel layout requires adapter pickup tiprack configuration', Error)
		
		#Try and pickup tip, if fails, then start refilling process#
		# Capture layout BEFORE pick_up_tip — OutOfTipsError can clear active_nozzles.
		_layout_before_refill = self._pipette_nozzle_layout_params(active_pipette)
		try:
			active_pipette.pick_up_tip(locus)
			return_code =  0
		except OutOfTipsError as Error:
			self._log('Out of tips, starting refilling process')
			refill_snap = self._build_refill_snapshot(tip_load_name, refill_all=refill_all)
			refill_snap.layout = _layout_before_refill
			return_code = self._handle_main_deck_exhausted(
				tip_load_name,
				active_pipette,
				refill_snap,
				locus,
				refill_all=refill_all,
			)

		#Return labware to the shuttle it it had to be moved to the open slot during a tip refill
		if self.return_to_stacker:
			self._log('Returning labware to stacker')
			stacker_original_labware, holding_slot, tip_load_name, chosen_index  = self.return_to_stacker
			self._shuttle_labware(stacker_original_labware,self.stackers[tip_load_name][chosen_index][0])
			self.return_to_stacker = False
		#Count the pick up for the tiptype and the pipette, return code for how a tip was picked up
		if tip_load_name in self.tip_counts.keys():
			self.tip_counts[tip_load_name] = self.tip_counts[tip_load_name] + active_pipette.active_channels
		else:
			self.tip_counts[tip_load_name] = active_pipette.active_channels
		self.pick_up_count[active_pipette] = self.pick_up_count[active_pipette] + 1
		return return_code
	

	def shuffle_for_forced_pickup(
		self,
		tip_load_name: str,
		pick_up_slot: str,
		pipette: protocol_api.InstrumentContext,
		*,
		layout: tuple[NozzleConfigurationType | None, str | None, str | None] | None = None,
	) -> None:
		'''
		This function will shuffle labware around the deck to force the next tip pickup for a rack type to be in its pick_up_slot. This function should generally only be used by the tracker itself \
		when a rack is out of tips and a manual refill is not needed. If there is a waste chute, the old labware will be thrown away. This function will update internal data after moving labware around the deck. \
		Forced pickup is most useful for partial tip pickups so that only the specified slots need spatial clearance for partial tip pickup.
		
		:param self: TipTracker object
		:param tip_load_name: The API load name of the tiprack type that should be shuffled into the forced pickup slot
		:type tip_load_name: str
		:param pick_up_slot: The old labware slot that should be disposed of or moved away in order to make room for the next tiprack. Which is generally the forced pickup slot for that rack
		:type pick_up_slot: str
		:param pipette: The pipette that you are assigning the tiprack to, used to update the tiprack list after shuffling to the force pickup type
		:type pipette: protocol_api.InstrumentContext
		:return: None
		:rtype: None
		'''
		if pick_up_slot in self.tiprack_adapters.keys():
			empty_rack = self.tiprack_adapters[pick_up_slot][1].child
		else:
			empty_rack = self.ctx.deck[pick_up_slot]
		next_rack = None
		for slot in self.rack_assignments[tip_load_name]:
			if slot == pick_up_slot:
				continue
			candidate = self.ctx.deck.get(slot)
			if candidate is None or not isinstance(candidate, protocol_api.Labware):
				continue
			if candidate.load_name == 'opentrons_flex_96_tiprack_adapter':
				candidate = candidate.child
			if (
				candidate is not None
				and candidate.load_name == tip_load_name
				and self._labware_is_on_deck(candidate)
				and any(well.has_tip for well in candidate.wells())
			):
				next_rack = candidate
				break
		if next_rack is None:
			raise ValueError(f"No other tiprack with tips found to shuffle into {pick_up_slot} for {tip_load_name}")
		if self.carousel_tips:
			self.carousel(empty_rack, next_rack)
		elif self.use_chute:
			self._log(f'Disposing of empty tiprack in {pick_up_slot} replacing with {next_rack.parent}')
			self.ctx.move_labware(empty_rack, self.waste, use_gripper=self.use_gripper)
			self.ctx.move_labware(
				next_rack, self.pick_up_slots[tip_load_name], use_gripper=self.use_gripper,
			)
		self._reassign_after_partial_pickup_refill(tip_load_name, pipette, layout)
	def _forced_pickup_slot(self, tip_load_name: str) -> str:
		"""Deck slot used for partial (locus) tip pickup for ``tip_load_name``."""
		if tip_load_name in self.pick_up_slots:
			return self.pick_up_slots[tip_load_name]
		assignments = self.rack_assignments.get(tip_load_name, [])
		if not assignments:
			raise ValueError(f'No rack assignments for {tip_load_name}')
		return assignments[0]
	def _forced_pickup_labware(self, tip_load_name: str) -> protocol_api.Labware:
		"""Labware on the forced partial-pickup slot (adapter child when applicable)."""
		slot = self._forced_pickup_slot(tip_load_name)
		if slot in self.tiprack_adapters:
			return self.tiprack_adapters[slot][1].child
		item = self.ctx.deck[slot]
		if item is None:
			raise ValueError(f'No tiprack on forced pickup slot {slot} for {tip_load_name}')
		if not isinstance(item, protocol_api.Labware):
			raise ValueError(f'Forced pickup slot {slot} holds a module, not tiprack labware')
		if item.load_name == 'opentrons_flex_96_tiprack_adapter':
			if item.child is None:
				raise ValueError(f'Tiprack adapter on {slot} has no child rack')
			return item.child
		return item
	def _collect_refill_targets(self, tip_load_name: str) -> tuple[list[str], list, list, list[str]]:
		"""Return slots_to_check, empty_tipracks, vacant_slots, empty_tiprack_slots."""
		slots_to_check = self._slots_for_rack_refill(tip_load_name)
		empty_tipracks = self._empty_tiprack_labware_for_type(tip_load_name)
		vacant_slots = self._vacant_slots_for_type(tip_load_name)
		empty_tiprack_slots = self._empty_tiprack_slot_ids(empty_tipracks)
		return slots_to_check, empty_tipracks, vacant_slots, empty_tiprack_slots
	def refill_forced_pickup_rack(
		self,
		tip_load_name: str,
		pipette: int | str | protocol_api.InstrumentContext | None = None,
	) -> protocol_api.Labware:
		"""Replace an exhausted partial-pickup tiprack without picking up a tip."""
		if pipette is not None:
			active_pipette = (
				self.pipette1 if pipette in (1, '1', self.pipette1, 'one', 'One')
				else self.pipette2 if pipette in (2, '2', self.pipette2, 'two', 'Two')
				else pipette
			)
		elif self.active_pipette is not None:
			active_pipette = self.active_pipette
		else:
			raise ValueError('refill_forced_pickup_rack: set active_pipette or pass pipette')

		pick_up_slot = self._forced_pickup_slot(tip_load_name)
		layout = self._pipette_nozzle_layout_params(active_pipette)

		self._log(f'Partial-pickup rack on {pick_up_slot} exhausted, shuffling or refilling {tip_load_name}')
		try:
			self.shuffle_for_forced_pickup(
				tip_load_name, pick_up_slot, active_pipette, layout=layout,
			)
			return self._forced_pickup_labware(tip_load_name)
		except ValueError:
			pass

		refill_snap = self._build_refill_snapshot(tip_load_name, refill_all=False)
		refill_snap.layout = layout
		self._handle_main_deck_exhausted(
			tip_load_name,
			active_pipette,
			refill_snap,
			locus=None,
			refill_all=False,
			pickup=False,
		)
		self._reassign_after_partial_pickup_refill(tip_load_name, active_pipette, layout)
		self.open_slot = self.original_open_slot
		return self._forced_pickup_labware(tip_load_name)


	def add_starting_tipracks(self, tiprack1 : str, slots1 : str | list[str],
						   	tiprack2 : str = None,slots2 : list[str] | str = None,
							tiprack3 : str = None, slots3 : str | list[str] = None,
							tiprack4 : str = None, slots4 : str | list[str] = None,
							max_racks_1 : int = None, max_racks_2 : int = None,
							max_racks_3 : int = None, max_racks_4 : int = None,
							adapters : list[str] = []) -> None:
		'''
		Load tipracks as a replacement for ProtocolContext.load_labware() for all tipracks and slots that you want to use at the beginning of the protocol. \
		This function will also assign the given slots for each tiprack as the slots to reload the tipracks onto, but this can be changed with assign_slots if needed. \
		Although this function only takes 4 tiprack-slot pairs, it can be used multiple times to load more tipracks or assign more slots. Four racks were chosen since \
		it is unlikely that one would use both filtertips and non filtertips at the same time, but is technically allowed by the tracker and are treated separately since they \
		have different API load names. The maximum racks of each type can be added here to prevent excess reloading of tipracks. The number of racks can be left as None for no limit. \
		The number of racks can be found by printing TipTracker.tip_counts and tip_rack_counts after a run to see how many racks were used and how many tips were used from each rack type.
		
		:param self: TipTracker object
		:param tiprack1: The API load name of the first tiprack to load onto the deck, i.e. 'opentrons_flex_96_tiprack_50ul'
		:type tiprack1: str
		:param slots1: The slot or list of slots to load tiprack1 onto, i.e. 'A1' or ['A1','B1','C1','D1']
		:type slots1: str | list[str]
		:param tiprack2: The API load name of the second tiprack to load onto the deck, i.e. 'opentrons_flex_96_tiprack_50ul'
		:type tiprack2: str
		:param slots2: The slot or list of slots to load tiprack2 onto, i.e. 'A2' or ['A2','B2','C2','D2']
		:type slots2: list[str] | str
		:param tiprack3: The API load name of the third tiprack to load onto the deck, i.e. 'opentrons_flex_96_tiprack_50ul'
		:type tiprack3: str
		:param slots3: The slot or list of slots to load tiprack3 onto, i.e. 'A3' or ['A3','B3','C3','D3']
		:type slots3: str | list[str]
		:param tiprack4: The API load name of the fourth tiprack to load onto the deck, i.e. 'opentrons_flex_96_tiprack_50ul'
		:type tiprack4: str
		:param slots4: The slot or list of slots to load tiprack4 onto, i.e. 'A4' or ['A4','B4','C4','D4']
		:type slots4: str | list[str]
		:param max_racks_1: The maximum number of tipracks of type tiprack1 that should be loaded onto the deck, if None there is no limit. This prevents reloading all slots when only one more would be needed
		:type max_racks_1: int
		:param max_racks_2: The maximum number of tipracks of type tiprack2 that should be loaded onto the deck, if None there is no limit. This prevents reloading all slots when only one more would be needed
		:type max_racks_2: int
		:param max_racks_3: The maximum number of tipracks of type tiprack3 that should be loaded onto the deck, if None there is no limit. This prevents reloading all slots when only one more would be needed
		:type max_racks_3: int
		:param max_racks_4: The maximum number of tipracks of type tiprack4 that should be loaded onto the deck, if None there is no limit. This prevents reloading all slots when only one more would be needed
		:type max_racks_4: int
		:param adapters: List of slots that should have adapters, if any. This is only needed to be used if you are loading tipracks onto adapter slots with this function,.
		:type adapters: list[str]
		:raises ValueError: If any slot is on the Flex expansion deck (A4, B4, C4, D4) but add_expansion_slots was not called for it first (enforced in load_tipracks).
		:return: None
		:rtype: None
		'''
		assign_slots = [slots1, slots2, slots3, slots4]
		tipracks = [tiprack1, tiprack2, tiprack3, tiprack4]
		try:
			for slot, rack in zip(assign_slots,tipracks):
				if slot != None and rack != None:
					continue
				elif slot == None and rack == None:
					continue
				else:
					raise ValueError(f"Tiprack {rack} and slots {slot} must be defined together")
		except ValueError as Error:
			self._fatal_tracker_error('add_starting_tipracks: tiprack and slots must be defined together for each pair', Error)
		for x, slot in enumerate(assign_slots):
			if type(slot) != list:
				assign_slots[x] = [slot]
		try:
			if len(set([tiprack for tiprack in tipracks if tiprack != None])) != len([tiprack for tiprack in tipracks if tiprack != None]):
				raise ValueError("Duplicate tiprack types detected, please ensure all tiprack slots are added under one tiprack argument")
			if len(set([slot for slot_list in assign_slots for slot in slot_list if slot != None])) != len([slot for slot_list in assign_slots for slot in slot_list if slot != None]):
				raise ValueError("Duplicate slots detected, please ensure all slots are unique across tiprack arguments")
		except ValueError as Error:
			self._fatal_tracker_error('add_starting_tipracks: duplicate tiprack types or duplicate slots across pairs', Error)

		for max_rack,tiprack in zip([max_racks_1, max_racks_2, max_racks_3, max_racks_4],tipracks):
			if max_rack != None and type(max_rack) != int:
				raise TypeError(f"Max racks must be an integer, got {type(max_rack)}")
			else:
				if max_rack != None:
					self.max_racks_count[tiprack] = max_rack
		self.load_tipracks(tiprack1,slots1,tiprack2,slots2,tiprack3,slots3,tiprack4,slots4, adapters=adapters)
		self.assign_slots(tiprack1,slots1,tiprack2,slots2,tiprack3,slots3,tiprack4,slots4)


	def reset_rack_list(self,rack_names : str | list[str] | None) -> None:
		'''
		Resets the internal data of the tracker for a given rack name or multiple rack names. This should be called after moving anything offdeck or on deck to prevent unaccessable tipracks from \
		being assigned to the pipettes. This essentially prevents "LabwareOffDeckError" by updating internal data to match the current deck state. This should be called after \
		any manual moves of tipracks or changes to the deck layout pertaining to any of the slots assigned to tipracks, but is generally not needed to be called directly. \
		If NoneType is passed, then all rack types in the internal data will be reset. This function does not reassign current slots to rack assignments. 
		
		:param self: TipTracker object
		:param rack_names: The API load name(s) of the rack(s) to reset, i.e. opentrons_flex_96_tiprack_50ul if None resets all rack types in internal data
		:type rack_names: str | list[str] | None
		:return: None
		:rtype: None
		'''
		if type(rack_names) == str:
			rack_names = [rack_names]
		elif rack_names is None:
			rack_names = list(set(list(self.tipracks.keys()) + list(self.ex_racks.keys())))
		for tip_load_name in rack_names:
			rack_list = []
			ex_list = []
			adapter_list = []
			for slot in self.rack_assignments.get(tip_load_name, []):
				item = self.ctx.deck.get(slot)
				if item is None:
					continue
				if slot in self.tiprack_adapters:
					rack_obj = self.tiprack_adapters[slot][1].child
					if (
						rack_obj is not None
						and rack_obj.load_name == tip_load_name
						and self._labware_is_on_deck(rack_obj)
					):
						adapter_list.append(rack_obj)
					continue
				# Modules (e.g. FlexStackerContext) sit on deck slots but have no load_name.
				if not isinstance(item, protocol_api.Labware):
					continue
				if item.load_name == 'opentrons_flex_96_tiprack_adapter':
					rack_obj = item.child
					if (
						rack_obj is not None
						and rack_obj.load_name == tip_load_name
						and self._labware_is_on_deck(rack_obj)
					):
						adapter_list.append(rack_obj)
				elif item.load_name == tip_load_name and self._labware_is_on_deck(item):
					if slot in self.ex_slots:
						ex_list.append(item)
					else:
						rack_list.append(item)
			self.tipracks[tip_load_name] = rack_list
			self.ex_racks[tip_load_name] = ex_list
			if adapter_list:
				self.adapter_pickup_tipracks[tip_load_name] = adapter_list
			elif tip_load_name in self.adapter_pickup_tipracks:
				del self.adapter_pickup_tipracks[tip_load_name]


	def add_expansion_slots(self, slots : str | list[str]) -> None:
		'''
		This function adds expansion slots [A4,B4,C4,D4] as available slots that should be tracked along with all the other default slots on deck. \
		This function should be called before assigning a tiprack to the any expansions slots. This function alone does not assign any tipracks to the expansion slots, \
		you must use the assign_slots function or with add_starting_tipracks to assign tipracks to the expansion slots after calling this function. \
		The expansion slots added with this function do not have to be ALL of the expansion slots installed on the robot, just the ones you want to reserve for tracking.
		
		:param self: TipTracker object
		:param slots: The expansion slots to add as available slots for tiprack loading and tracking, can be a list of strings or a single string, valid inputs are 'A4','B4','C4', and 'D4'
		:type slots: str | list[str]
		:return: None
		:rtype: None
		'''
		if isinstance(slots, str):
			to_add = [slots]
		elif isinstance(slots, list):
			to_add = slots
		else:
			raise TypeError("Expansion slots must be a string or list of strings")
		if not self.ex_slots:
			self.ex_slots = to_add
		else:
			self.ex_slots.extend(to_add)
		self.ex_slots = list(set(self.ex_slots))
		invalid_slots = [x for x in self.ex_slots if x not in self.EXPANSION_DECK_SLOTS]
		if len(invalid_slots) > 0:
			raise ValueError(f"Invalid expansion slots: {invalid_slots}, slots must be A4, B4, C4, or D4")
		self._log(f'TipTracker: expansion slots registered: {sorted(self.ex_slots)}')
			

	def drop_tip(self, pipette : int | str | protocol_api.InstrumentContext = None, locus : protocol_api.Labware | protocol_api.Well | None = None, return_tip : bool = False) -> None:
		'''
		Drop tip at a specified locus for a specified pipette. This is to replace the pipette.drop_tip() method to make it easier to conditionally return tips or drop them in a waste bin \
		or a waste chute. Ensure you are not in partial tip configurations when setting return tip to True or it will cause an error. With no arguments passed, it will drop the tip of the \
		active pipette in the designated trash. My general use is TrackerObject.drop_tip(return_tip=DryRunParameter) if an active pipette is set in the code prior to calling this function.
		
		:param self: TipTracker object
		:param pipette: The pipette you want the tip to be removed for. If None then it will use TrackerObject.active_pipette, which should be set beforehand. Can be specified as the pipette object or as an integer (1 or 2) or string ('one' or 'two' or '1' or '2')
		:type pipette: int | str | protocol_api.InstrumentContext
		:param locus: Where the tip should be dropped or returned to, if None will drop at default waste bin or return to tiprack depending on the return_tip parameter.
		:type locus: protocol_api.Labware | protocol_api.Well | None
		:param return_tip: If the tip should be returned to its origin instead of dropping it at the waste bin
		:type return_tip: bool
		:return: None
		:rtype: None
		'''
		if pipette != None:
			pip = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
			if pip == None:
				if pipette in (2, '2', 'two', 'Two') and self.pipette2 is None:
					raise ValueError(
						"Requested pipette 2 but TipTracker has no second pipette (pipette2=None). "
						"Use 1, omit the pipette argument, or pass the pipette object."
					)
				raise ValueError(f"Invalid pipette number {pipette}, must be 1 or 2, as strings or integers or pipette objects")
		elif pipette == None and self.active_pipette != None:
			pip = self.active_pipette
		if pip == None and self.active_pipette == None:
			raise ValueError(f"Active Pipette not set, please specify pipette or set active pipette beforehand")
		self.drop_count[pip] = self.drop_count[pip] + 1
		if return_tip:
			pip.return_tip(locus)
		else:
			pip.drop_tip(locus)

			
	def replace_tips(self,old_rack_name : str, new_rack_name : str , number_to_replace : int | None = None, manually_remove = True) -> None:
		'''
		Remove a certain number (or all) of a specified tiprack type to replace with a new type. \
		Useful when you no longer need a type of tip on deck and you want the space for something else. \
		By default this will cause the protocol to pause and prompt the user to replace all tipracks of Type A with Type B \
		and then assign all of the given slots to the new rack type in case another refill in needed later.

		:param self: TipTracker object
		:param old_rack_name: API load name of the tiprack type to replace
		:type old_rack_name: str
		:param new_rack_name:  str of the new tiprack load name
		:type new_rack_name: str
		:param number_to_replace: int of how many to replace, if None will replace all of that type
		:type number_to_replace: int | None
		:param manually_remove: If you want to manually remove the old racks from the deck instead of using the waste chute. On by default since they have to be manually replaced and protocol needs to be paused
		:type manually_remove: bool
		:return: None
		:rtype: None
		'''
		self._log(f'Replacing {number_to_replace} {old_rack_name} with {new_rack_name}')
		slot_list = self.rack_assignments[old_rack_name][:number_to_replace]
		self._log('Replacing tipracks')
		self.ctx.home()
		self.clear_old(old_rack_name,slot_list,manually_remove)
		existing_new = list(self.rack_assignments.get(new_rack_name, []))
		new_rack_slot_list = existing_new + [s for s in slot_list if s not in existing_new]
		old_rack_slot_list = [] if number_to_replace is None else self.rack_assignments[old_rack_name][number_to_replace:]
		self.assign_slots(tiprack1=new_rack_name,slots1=new_rack_slot_list,
						tiprack2=old_rack_name,slots2=old_rack_slot_list)
		self.load_tipracks(new_rack_name,slot_list)


	def refill_tips(self, tip_load_name : str , slots : list[str] | str, waste_all_old : bool = True) -> None:
		'''
		This function refills tipracks of a given API load name on the given slots. It will first clear **exhausted** tipracks from the deck, \
		by moving them to the waste chute or off deck, then load new racks onto slots that need them. Racks that still have tips are left in place. \
		Any slots in the ignore_slots list will be ignored for refilling, so if you have a rack that you want to stay on the deck (tip reuse), \
		add its slot to the ignore_slots list and it will be skipped over when refilling. \
		
		:param self: TipTracker object
		:param tip_load_name: API load name for the tiprack that you want to refill, for example opentrons_flex_96_filtertip_50ul
		:type tip_load_name: str
		:param slots: List or string of slots involved in this refill (typically rack assignment slots minus ignore_slots)
		:type slots: list[str] | str
		:param waste_all_old: If True, scan all slots assigned to this tiprack type for exhausted racks to clear; if False, only scan the slots in ``slots``.
		:type waste_all_old: bool
		:return: None
		:rtype: None
		'''
		if self.ignore_slots != []:
			if type(slots) == list:
				slots = [slot for slot in slots if slot not in self.ignore_slots]
			elif type(slots) == str:
				if slots in self.ignore_slots:
					slots = None
			else:
				raise TypeError(f"Slots must be a string or list of strings, got {type(slots)}")
			self._log(f'Ignoring slots {self.ignore_slots} for refill')
		if slots is None or slots == []:
			self._log(f'No slots left to refill for {tip_load_name} after applying ignore_slots; skipping refill_tips')
			return
		slot_list = [slots] if isinstance(slots, str) else list(slots)
		self._log(f'Refilling tips of {tip_load_name} on {slot_list}')
		if waste_all_old:
			candidate_slots = [s for s in self.rack_assignments.get(tip_load_name, []) if s not in self.ignore_slots]
		else:
			candidate_slots = [s for s in slot_list if s not in self.ignore_slots]
		clear_slots = [s for s in candidate_slots if self._slot_has_empty_tiprack_of_type(s, tip_load_name)]
		self.clear_old(tip_load_name, clear_slots, False)
		load_slots = list(dict.fromkeys(clear_slots + [s for s in slot_list if self.ctx.deck[s] is None]))
		self.load_tipracks(tip_load_name, load_slots)

	def _slots_for_rack_refill(self, tip_load_name: str) -> list[str]:
		"""Assigned slots for ``tip_load_name`` excluding ``ignore_slots`` (same basis as pick-up refills; may include expansion row)."""
		return [slot for slot in self.rack_assignments[tip_load_name] if slot not in self.ignore_slots]

	def _coerce_slot_list(self, slots: str | list[str]) -> list[str]:
		"""Normalize a single slot or list into a list of strings."""
		return [slots] if isinstance(slots, str) else list(slots)

	def _require_expansion_registered(self) -> None:
		if not self.ex_slots:
			raise ValueError('Expansion slots are not registered; call add_expansion_slots() first.')

	def _slots_assigned_expansion(self, tip_load_name: str) -> list[str]:
		"""Subset of ``rack_assignments`` on registered expansion slots (A4–D4) and not in ``ignore_slots``."""
		if not self.ex_slots:
			return []
		ex = self.ex_slots
		return [
			slot for slot in self.rack_assignments.get(tip_load_name, [])
			if slot in ex and slot not in self.ignore_slots
		]

	def _slots_assigned_main_deck(self, tip_load_name: str) -> list[str]:
		"""Assigned slots that are not expansion slots and not ``ignore_slots``."""
		ex = self.ex_slots
		return [
			slot for slot in self.rack_assignments.get(tip_load_name, [])
			if slot not in ex and slot not in self.ignore_slots
		]

	def _operator_refill_impl(
		self,
		tip_load_name: str,
		slot_list: list[str],
		*,
		skip_message: str,
		pause_place_clause: str,
		pipette: int | str | protocol_api.InstrumentContext | None,
		reassign_pipette: bool,
	) -> None:
		if not slot_list:
			self._log(skip_message)
			return
		count_before_refill = self.tip_rack_counts.get(tip_load_name, 0)
		load_plan = self._planned_refill_load_slots(tip_load_name, slot_list, waste_all_old=True)
		self.refill_tips(tip_load_name, slot_list)
		self.ctx.home()
		display_slots = self._cap_slots_to_max_rack_budget(
			tip_load_name, load_plan, count_before=count_before_refill
		)
		if display_slots:
			self.ctx.pause(f'Please place {tip_load_name} {pause_place_clause} {display_slots}')
		if pipette is not None and reassign_pipette:
			resolved_pipette = (
				self.pipette1
				if pipette in (1, '1', self.pipette1, 'one', 'One')
				else self.pipette2
				if pipette in (2, '2', self.pipette2, 'two', 'Two')
				else pipette
			)
			self._reassign_preserving_layout(tip_load_name, resolved_pipette)


	def _reload_after_pause_if_non_empty(
		self, tip_load_name: str, load_slots: list[str], *, skip_message: str
	) -> None:
		if not load_slots:
			self._log(skip_message)
			return
		self._reload_tipracks_after_pause(tip_load_name, load_slots)

	def refill_deck(
		self,
		tip_load_name: str,
		pipette: int | str | protocol_api.InstrumentContext | None = None,
		slots: str | list[str] | None = None,
		*,
		reassign_pipette: bool = True,
	) -> None:
		'''Operator refill for tipracks on the main deck: clear exhausted racks on the target slots, home, pause for the user to place full racks, then optionally reassign that tip type to a pipette.'''
		if slots is None:
			slot_list = self._slots_for_rack_refill(tip_load_name)
		else:
			slot_list = [s for s in self._coerce_slot_list(slots) if s not in self.ignore_slots]
		self._operator_refill_impl(
			tip_load_name,
			slot_list,
			skip_message=f'No deck slots to refill for {tip_load_name} after ignore_slots; skipping refill_deck',
			pause_place_clause='onto slots',
			pipette=pipette,
			reassign_pipette=reassign_pipette,
		)

	def refill_main_deck_slots(
		self,
		tip_load_name: str,
		pipette: int | str | protocol_api.InstrumentContext | None = None,
		slots: str | list[str] | None = None,
		*,
		reassign_pipette: bool = True,
	) -> None:
		'''Same as ``refill_deck`` but only slots **not** on the expansion row (column 4 staging).'''
		ex = self.ex_slots
		if slots is None:
			slot_list = self._slots_assigned_main_deck(tip_load_name)
		else:
			slot_list = [s for s in self._coerce_slot_list(slots) if s not in self.ignore_slots and s not in ex]
		self._operator_refill_impl(
			tip_load_name,
			slot_list,
			skip_message=f'No main-deck slots to refill for {tip_load_name}; skipping refill_main_deck_slots',
			pause_place_clause='onto slots',
			pipette=pipette,
			reassign_pipette=reassign_pipette,
		)

	def refill_expansion_slots(
		self,
		tip_load_name: str,
		pipette: int | str | protocol_api.InstrumentContext | None = None,
		slots: str | list[str] | None = None,
		*,
		reassign_pipette: bool = True,
	) -> None:
		'''
		Operator refill for **expansion** slots only (registered via ``add_expansion_slots`` and assigned for this tip type).

		Raises ``ValueError`` if expansion slots were never registered — call ``add_expansion_slots`` first.
		'''
		self._require_expansion_registered()
		ex = self.ex_slots
		if slots is None:
			slot_list = self._slots_assigned_expansion(tip_load_name)
		else:
			slot_list = [s for s in self._coerce_slot_list(slots) if s in ex and s not in self.ignore_slots]
		self._operator_refill_impl(
			tip_load_name,
			slot_list,
			skip_message=f'No expansion slots assigned for {tip_load_name}; skipping refill_expansion_slots',
			pause_place_clause='onto expansion slots',
			pipette=pipette,
			reassign_pipette=reassign_pipette,
		)

	def _reload_tipracks_after_pause(self, tip_load_name: str, load_slots: list[str]) -> None:
		self.ctx.home()
		count_before = self.tip_rack_counts.get(tip_load_name, 0)
		needs_place = [s for s in load_slots if self._slot_needs_tiprack_load(s, tip_load_name)]
		display_slots = self._cap_slots_to_max_rack_budget(
			tip_load_name, needs_place, count_before=count_before
		)
		if not display_slots:
			self._ensure_adapters_stocked(tip_load_name)
			return
		adapter_note = [
			s for s in display_slots
			if s in self.tiprack_adapters or (
				self.ctx.deck.get(s) is not None
				and getattr(self.ctx.deck.get(s), 'load_name', None) == 'opentrons_flex_96_tiprack_adapter'
			)
		]
		msg = f'Place {tip_load_name} onto slots {display_slots}'
		if adapter_note:
			msg += f' (mount on tiprack adapter in {adapter_note})'
		self.ctx.pause(msg)
		self.load_tipracks(tip_load_name, display_slots)
		self._ensure_adapters_stocked(tip_load_name)


	def reload_deck_tipracks(self, tip_load_name: str, slots: str | list[str] | None = None) -> None:
		'''
		Home, pause for the user to place racks, then ``load_tipracks`` for those slots. Used when internal supply (expansion/stacker) is exhausted and assigned deck slots must be repopulated.

		Does not call ``reset_rack_list`` or ``assign_tipracks``; callers (such as ``pick_up``) should do that after this when wiring the full refill sequence.
		'''
		if slots is None:
			load_slots = list(dict.fromkeys(self.rack_assignments[tip_load_name]))
		else:
			load_slots = list(dict.fromkeys(self._coerce_slot_list(slots)))
		self._reload_tipracks_after_pause(tip_load_name, load_slots)

	def reload_expansion_tipracks(self, tip_load_name: str, slots: str | list[str] | None = None) -> None:
		'''
		Home, pause, then ``load_tipracks`` for **expansion** assignments only. Does not reset or reassign pipettes.

		Raises ``ValueError`` if expansion slots were never registered.
		'''
		self._require_expansion_registered()
		ex = self.ex_slots
		if slots is None:
			load_slots = self._slots_assigned_expansion(tip_load_name)
		else:
			load_slots = [s for s in self._coerce_slot_list(slots) if s in ex]
		self._reload_after_pause_if_non_empty(
			tip_load_name,
			load_slots,
			skip_message=f'No expansion load slots for {tip_load_name}; skipping reload_expansion_tipracks',
		)

	def reload_main_deck_tipracks(self, tip_load_name: str, slots: str | list[str] | None = None) -> None:
		'''Home, pause, then ``load_tipracks`` for main-deck assignments only (excludes expansion row).'''
		ex = self.ex_slots
		if slots is None:
			load_slots = self._slots_assigned_main_deck(tip_load_name)
		else:
			load_slots = [s for s in self._coerce_slot_list(slots) if s not in ex]
		self._reload_after_pause_if_non_empty(
			tip_load_name,
			load_slots,
			skip_message=f'No main-deck load slots for {tip_load_name}; skipping reload_main_deck_tipracks',
		)


	def waste_tips(self, slots : str | list[str] | protocol_api.Labware) -> None:
		'''
		This function throws tipracks into the waste chute or pauses to move them off deck if no chute or gripper is present. This function\
		does not inherently change the internal data, so it can be used independently of the refilling functions to just move old racks out of the way. \
		Although its uses are niche, and within the majority of this code refill_tips is called right after to add the new tipracks to the deck \
		ADAPTERS NOT CURRENTLY SUPPORTED FOR TIPRACKS IN ALL PARTS OF CODE
		
		:param self: TipTracker object
		:param slots: The slot(s) to move the old tiprack(s) out of, can be a string for one slot, a list of strings for multiple slots or a labware object if using adapters 
		:type slots: str | list[str] | protocol_api.Labware
		:return: None
		:rtype: None
		'''
		self._log(f'Wasting tips on slots {slots}: Using gripper : {self.use_gripper}')
		if type(slots) == str or type(slots) == protocol_api.Labware:
			slots = [slots]
		destination = self.waste if self.use_chute else protocol_api.OFF_DECK
		_offdeck = type(protocol_api.OFF_DECK)
		for slot in slots:
			if isinstance(slot, _offdeck):
				continue
			slot_key = self._deck_slot_id(slot) if isinstance(slot, protocol_api.Labware) else slot
			if isinstance(slot_key, _offdeck):
				continue
			if slot_key in self.ignore_slots:
				self._log(f'Ignoring slot {slot_key} for waste tips')
				continue
			if slot_key in self.tiprack_adapters.keys():
				labware_to_move = self.tiprack_adapters[slot_key][1].child
			elif type(slot) == protocol_api.Labware:
				if slot.load_name != 'opentrons_flex_96_tiprack_adapter':
					labware_to_move = slot
				else:
					labware_to_move = slot.child
			else:
				if slot_key not in self.ctx.deck:
					continue
				labware_to_move = self.ctx.deck[slot_key]
			self.ctx.move_labware(labware_to_move, destination,use_gripper=self.use_gripper)


	def assign_tipracks(self, rack_name : str,pipette : int | str | protocol_api.InstrumentContext = None, mode : NozzleConfigurationType = None, start : str = None, end : str = None) -> None:
		'''
		Assign specified tipracks to a specified pipette or self.active_pipette if none specified.\
		Instead of pip.tip_racks = [tipracks], use TipTrackerObject.assign_tipracks(opentrons_flex_96_filtertip_50ul,protocol_api.InstrumentContext).\
		If a mode is provided, it will reconfigure the pipette active nozzle layout and assign the correct tipracks \
		(e.g. adapters for 96-channel layouts). Mode, start, and end are ignored unless a nozzle style is specified.

		:param self: TipTracker object
		:param rack_name: API Load name for the tiprack that you want to use, for example opentrons_flex_96_filtertip_50ul
		:type rack_name: str
		:param pipette: The pipette that you want to assign the chosen tipracks to. Can be specified as the pipette object itself or as 1 or 2 corresponding to which order you loaded them in. If None uses the active pipette
		:type pipette: int | str | protocol_api.InstrumentContext | None
		:param mode: Nozzle layout style: ALL, COLUMN, ROW, SINGLE, or PARTIAL_COLUMN (see Opentrons API). If None, tip racks are assigned without changing nozzle layout.
		:type mode: NozzleConfigurationType | None
		:param start: The starting nozzle for PARTIAL_COLUMN layout; ignored otherwise.
		:type start: str | None
		:param end: The ending nozzle for PARTIAL_COLUMN layout; ignored otherwise.
		:type end: str | None
		:return: None
		:rtype: None
		'''
		if pipette == None and self.active_pipette == None:
			raise ValueError(f"Active Pipette not defined correctly, please specify pipette or set active pipette: {self.active_pipette}")
		
		if pipette != None:
			resolved_pipette = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
			if resolved_pipette == None:
				if pipette in (2, '2', 'two', 'Two') and self.pipette2 is None:
					raise ValueError(
						"Requested pipette 2 but TipTracker has no second pipette (pipette2=None). "
						"Use 1, omit the pipette argument, or pass the pipette object."
					)
				raise ValueError(f"Invalid pipette number {pipette}, must be 1 or 2, as strings or integers or pipette objects")
		else:
			resolved_pipette = self.active_pipette
		self._log(f'Reassigning tipracks of {resolved_pipette} to {rack_name} with mode: {mode}')
		if resolved_pipette == self.pipette1:
			self.pipette_1_tip_type = rack_name
		elif resolved_pipette == self.pipette2:
			self.pipette_2_tip_type = rack_name
		# Remember explicit layouts so refill can restore them (active_nozzles is often unset).
		if mode is not None:
			self._pipette_layouts[resolved_pipette] = (mode, start, end)
		try:
			if mode in ( COLUMN, SINGLE, ROW, PARTIAL_COLUMN):
				accessible = self._accessible_tipracks(rack_name)
				resolved_pipette.configure_nozzle_layout(style=mode,start=start,end=end,tip_racks=accessible)
				if resolved_pipette.tip_racks == [] and self.ex_racks.get(rack_name,[]) == [] and rack_name not in self.stackers.keys():
					self._refill_deck_manually(self.rack_assignments[rack_name],rack_name)
					self.assign_tipracks(rack_name,resolved_pipette,mode,start,end)
			elif mode == ALL:
				if self._pipette_is_flex_96channel(resolved_pipette):
					if self.global_adapter and self.tiprack_adapters:
						self._mount_tip_type_on_global_adapter(rack_name)
					elif self._adapter_pickup_configured(rack_name) and self.global_adapter:
						self.assign_slots(rack_name, list(self.tiprack_adapters.keys())[0])
					if self._adapter_pickup_configured(rack_name):
						_tr = [
							r for r in self.adapter_pickup_tipracks.get(rack_name, [])
							if hasattr(r, 'wells') and self._labware_is_on_deck(r)
						]
						if not _tr:
							_tr = self._accessible_tipracks(rack_name)
					else:
						_tr = self._accessible_tipracks(rack_name)
					resolved_pipette.configure_nozzle_layout(style=ALL, start=start, end=end, tip_racks=_tr)
				else:
					resolved_pipette.tip_racks = self._accessible_tipracks(rack_name)
			elif mode is None:
				if self._pipette_is_flex_96channel(resolved_pipette) and self._adapter_pickup_configured(rack_name):
					resolved_pipette.tip_racks = self._accessible_adapter_tipracks(rack_name)
				else:
					resolved_pipette.tip_racks = self._accessible_tipracks(rack_name)
		except KeyError as Error:
			self._fatal_tracker_error(
				f'assign_tipracks: missing tiprack data for mode {mode!r} (load tipracks first and match rack_name to deck state)',
				Error,
			)
		


	def _clear_old_use_gripper_to_waste(self, save_tips: bool) -> bool:
		'''When False-ish ``save_tips`` and chute + gripper are on, racks are discarded automatically instead of pausing for manual removal.'''
		return save_tips is False and self.use_chute and self.use_gripper

	def _clear_old_resolve_slot_targets(self, tip_load_name: str, slots_to_clear: list | None) -> tuple[list[str], bool]:
		'''
		Return ``(slots, full_clear)``.
		``full_clear`` is True when ``slots_to_clear`` was None (every assigned slot that still holds something on the deck).
		'''
		if slots_to_clear is None:
			slots = [
				slot
				for slot in self.rack_assignments.get(tip_load_name, [])
				if self.ctx.deck[slot] is not None and slot not in self.ignore_slots
			]
			return slots, True
		return list(slots_to_clear), False

	def _rack_labware_on_slot_for_clear(self, slot: str) -> protocol_api.Labware | None:
		if slot in self.tiprack_adapters:
			return self.tiprack_adapters[slot][1].child
		return self.ctx.deck[slot]

	def _rack_is_on_stacker_or_module_shuttle(self, rack: protocol_api.Labware) -> bool:
		'''Skip labware that is actually a module handle (e.g. stacker shuttle) so we do not move it incorrectly.'''
		return rack in self.ctx.loaded_modules.values()

	def _move_rack_core_clear_old(self, rack: protocol_api.Labware, destination, use_gripper: bool) -> None:
		self._log(f'Moving {rack} to {destination}, use_gripper={use_gripper}')
		self.ctx._core.move_labware(
			labware_core=rack._core,
			new_location=destination,
			use_gripper=use_gripper,
			pause_for_manual_move=False,
			pick_up_offset=(0.0, 0.0, 0.0),
			drop_offset=(0.0, 0.0, 0.0),
		)

	def _clear_old_reset_all_tracking_for_type(self, tip_load_name: str) -> None:
		self.tipracks[tip_load_name] = []
		if tip_load_name in self.ex_racks:
			self.ex_racks[tip_load_name] = []
		if tip_load_name in self.adapter_pickup_tipracks:
			self.adapter_pickup_tipracks[tip_load_name] = []

	def _clear_old_partial_slots(
		self,
		tip_load_name: str,
		slots: list[str],
		toss_location,
		use_gripper: bool,
	) -> None:
		if not (tip_load_name in self.tipracks or tip_load_name in self.ex_racks or tip_load_name in self.adapter_pickup_tipracks):
			raise KeyError(f'Tiprack {tip_load_name} not found in tipracks / ex_racks / adapter_pickup_tipracks')
		pop_active: list[int] = []
		pop_expansion: list[int] = []
		pop_adapter: list[int] = []
		labware_to_move: list[protocol_api.Labware] = []
		for slot in slots:
			if slot in self.tiprack_adapters:
				rack = self.tiprack_adapters[slot][1].child
				for i, item in enumerate(self.adapter_pickup_tipracks[tip_load_name]):
					if item == rack:
						pop_adapter.append(i)
			else:
				rack = self.ctx.deck[slot]
				if slot in self.ex_slots:
					for i, item in enumerate(self.ex_racks[tip_load_name]):
						if item == rack:
							pop_expansion.append(i)
				else:
					for i, item in enumerate(self.tipracks[tip_load_name]):
						if item == rack:
							pop_active.append(i)
			if self._rack_is_on_stacker_or_module_shuttle(rack):
				continue
			labware_to_move.append(rack)
		pop_active.sort(reverse=True)
		pop_expansion.sort(reverse=True)
		pop_adapter.sort(reverse=True)
		for labware in labware_to_move:
			self._move_rack_core_clear_old(labware, toss_location, use_gripper)
		for i in pop_active:
			self.tipracks[tip_load_name].pop(i)
		for i in pop_expansion:
			self.ex_racks[tip_load_name].pop(i)
		for i in pop_adapter:
			self.adapter_pickup_tipracks[tip_load_name].pop(i)

	def clear_old(self, tip_load_name: str, slots_to_clear: None | list = None, save_tips: bool = True) -> None:
		'''Remove tip racks of ``tip_load_name`` from the deck (including adapters / expansion) and align TipTracker bookkeeping.'''
		self._log(f'Clearing old tipracks of {tip_load_name}')

		slots, full_clear = self._clear_old_resolve_slot_targets(tip_load_name, slots_to_clear)
		use_gripper = self._clear_old_use_gripper_to_waste(save_tips)
		toss_location = self.waste if use_gripper else protocol_api.OFF_DECK

		if use_gripper:
			self._log('Using gripper to remove tip racks')
		else:
			where = 'All slots' if full_clear else str(slots)
			self._log(f'Please remove all {tip_load_name} from {where}')
			self.ctx.pause(f'Please remove all {tip_load_name} from {where}')

		if full_clear:
			for slot in slots:
				rack = self._rack_labware_on_slot_for_clear(slot)
				if rack is None or self._rack_is_on_stacker_or_module_shuttle(rack):
					continue
				self._move_rack_core_clear_old(rack, toss_location, use_gripper)
			self._clear_old_reset_all_tracking_for_type(tip_load_name)
		else:
			self._clear_old_partial_slots(tip_load_name, slots, toss_location, use_gripper)


	def carousel(self, tiprack_to_move_away : protocol_api.Labware | str,tiprack_to_move_in : protocol_api.Labware | str) -> None:
		'''
		Carousel tipracks or labware around the deck using the open_slot as an intermediate location. Moves the tiprack_to_move_away to the open_slot,\
		then moves the tiprack_to_move_in to the slot vacated by tiprack_to_move_away. Finally, updates the open_slot to be the slot vacated by tiprack_to_move_in. \
		This is used a lot in the case of not wanting to use a waste chute and needing to move tipracks around the deck to free up space. \
		
		:param self: TipTracker object
		:param tiprack_to_move_away: The labware to move away from its current slot into the open slot or the deck slot as string that you want to clear
		:type tiprack_to_move_away: protocol_api.Labware | str
		:param tiprack_to_move_in: The labware to move into the slot vacated by tiprack_to_move_away. If a string is passed it must be the deck slot of whatever labware you are trying to move into the vacated slot
		:type tiprack_to_move_in: protocol_api.Labware | str
		:return: None
		:rtype: None
		'''
		if self.open_slot != None:
			open_slot = self.open_slot
		else:
			raise ValueError("No open slot defined, please define an open slot to move the tiprack to")
		
		if type(tiprack_to_move_away) == str:
			if tiprack_to_move_away in self.tiprack_adapters.keys():
				intermediate_slot = self.tiprack_adapters[tiprack_to_move_away][1]
				tiprack_to_move_away = self.tiprack_adapters[tiprack_to_move_away][1].child
			else:
				intermediate_slot = tiprack_to_move_away
				tiprack_to_move_away = self.ctx.deck[intermediate_slot]
		elif type(tiprack_to_move_away) == protocol_api.Labware:
			if tiprack_to_move_away.load_name == 'opentrons_flex_96_tiprack_adapter':
				intermediate_slot = tiprack_to_move_away
				tiprack_to_move_away = tiprack_to_move_away.child
			else:
				intermediate_slot = tiprack_to_move_away.parent
		if type(tiprack_to_move_in) == str:
			if tiprack_to_move_in in self.tiprack_adapters.keys():
				leaving_open_slot = self.tiprack_adapters[tiprack_to_move_in][1]
				tiprack_to_move_in = self.tiprack_adapters[tiprack_to_move_in][1].child
			else:
				leaving_open_slot = tiprack_to_move_in
				tiprack_to_move_in = self.ctx.deck[leaving_open_slot]
		elif type(tiprack_to_move_in) == protocol_api.Labware:
			if tiprack_to_move_in.load_name == 'opentrons_flex_96_tiprack_adapter':
				leaving_open_slot = tiprack_to_move_in
				tiprack_to_move_in = tiprack_to_move_in.child
			else:
				leaving_open_slot = tiprack_to_move_in.parent

		if tiprack_to_move_away.load_name in self.storing_stackers.keys():
			self._log(f'Storing {tiprack_to_move_away} in stacker {self.storing_stackers[tiprack_to_move_away.load_name]} to free up space for carousel')
			for x,(stacker_info) in enumerate(self.storing_stackers[tiprack_to_move_away.load_name]):
				if stacker_info[1] < 6:
					open_slot = stacker_info[0]
					self.storing_stackers[tiprack_to_move_away.load_name][x][1] = self.storing_stackers[tiprack_to_move_away.load_name][x][1] + 1
					break
			intermediate_slot = self.storing_stackers[tiprack_to_move_away.load_name]
		#Move old labware to open slot
		self._log(f' Carousel from {tiprack_to_move_away} on {intermediate_slot} to {self.open_slot}')
		self._shuttle_labware(tiprack_to_move_away,open_slot)
		#Move new labware into vacated slot
		self._log(f' Carousel from {tiprack_to_move_in} on {leaving_open_slot} to {intermediate_slot}')
		self._shuttle_labware(tiprack_to_move_in,intermediate_slot)
		#Store labware in stacker in needed
		if tiprack_to_move_away.load_name in self.storing_stackers.keys():
			self.store_in_stacker(tiprack_to_move_away,open_slot)
		#Change the open slot to the slot vacated by the new labware
		self._log(f'----->Assigning open_slot to {leaving_open_slot}')
		self.open_slot = leaving_open_slot


	def move_from_stacker(self,tip_load_name : str) -> protocol_api.Labware:
		'''
		Grab the next tiprack available from the any stacker module that has tipracks of the given tip_load_name. If a tiprack has a lid, it will be removed and placed in the waste bin. If the stacker has the proper racktype on the shuttle, it will return the shuttle labware. If the stacker has a different labware on it, it will move it to the open_slot first. \
		Returns the labware object retrieved from the stacker. It will use all of the tipracks in one stacker before moving to the next stacker of the same labware type
		
		:param self: TipTracker object
		:param tip_load_name: The API load name of the labware (tiprack) to retrieve from the stacker
		:type tip_load_name: str
		:return: Returns the next available labware from the stacker of the given tip_load_name
		:rtype: Labware
		'''
		stacker = None
		chosen_index = None
		supplies = self._stacker_supplies(tip_load_name)
		for x, supply in enumerate(supplies):
			if supply.rack_count > 0:
				stacker = supply.module
				chosen_index = x
				break
		if stacker is None:
			raise ValueError(f"No tipracks remaining in stackers for {tip_load_name}")
		stacker_current_labware = stacker.labware
		if stacker_current_labware == None:
			self._log(f'Retrieving labware from stacker for {tip_load_name}')
			labware = stacker.retrieve()
			supplies[chosen_index].rack_count -= 1
			self._write_stacker_supplies(tip_load_name, supplies)
			if supplies[chosen_index].has_lid:
				self._log(f'Removing lid from stacker for {tip_load_name}')
				self.ctx.move_lid(labware,self.waste,use_gripper=self.use_gripper)
		
		else:
			if stacker_current_labware.load_name != tip_load_name:
				self._log(f'Labware on stacker is not {tip_load_name}, moving to {self.open_slot} and retrieving new labware')
				if self.open_slot == None:
					raise ValueError("No open slot defined, please define an open slot to move the labware to with non-matching labware on shuttle")
				self._shuttle_labware(stacker_current_labware,self.open_slot)
				self.return_to_stacker = (stacker_current_labware,self.open_slot,tip_load_name,chosen_index)
				labware = stacker.retrieve()
				supplies[chosen_index].rack_count -= 1
				self._write_stacker_supplies(tip_load_name, supplies)
				if supplies[chosen_index].has_lid: 
					self._log(f'Removing lid from stacker for {tip_load_name}')
					self.ctx.move_lid(labware,self.waste,use_gripper=self.use_gripper)
			else:
				self._log(f'Getting labware already on shuttle for {tip_load_name}')
				labware = stacker_current_labware #If there is a tiprack on the stacker
		return labware
	

	def store_in_stacker(self,labware : protocol_api.Labware, store_stacker : protocol_api.FlexStackerContext, force_store : bool = False) -> None:
		'''
		Store in stacker is the reverse function of grab_from_stacker. It will take a given labware and store it in a stacker meant to be used for empty tipracks. It will not automatically add empty tipracks \
		to any stacker or any empty stacker, only a stacker specifically designated to hold empty tipracks. If provided it will replace the tiprack load_name with the one in the stacker for in the case \
		you want every empty tiprack type to be stored in the same stacker (this ruins labware count tracking for the end user but provides more flexibility with waste)
		
		:param self: TipTracker object
		:param labware: Labware to store in the stacker
		:type labware: protocol_api.Labware
		:param store_stacker: Target Flex stacker module context
		:type store_stacker: protocol_api.FlexStackerContext
		:param force_store: If True, store even when load names differ
		:type force_store: bool
		'''
		if labware.parent != store_stacker:
			self._shuttle_labware(labware,store_stacker) #Move labware to stacker if not already on it
		stored = store_stacker.get_stored_labware()
		if not stored:
			store_stacker.store()
		else:
			if not force_store and labware.load_name != stored[0].load_name:
				raise ValueError(f"Labware load name {labware.load_name} does not match stacker stored labware {stored[0]}, if you want to store this labware in the stacker anyway set force_store to True")
			if not force_store and labware.load_name == stored[0].load_name:
				store_stacker.store()
		
	
	def add_stacker(self, slot : str, tip_load_name : str, initial_count : int, lid : str | None, load_on_shuttle : bool = True, use_for_storing_empty : bool = False) -> protocol_api.FlexStackerContext:
		'''
		Load a stacker module on the deck and fill it with an initial number of tipracks. This replaces using ctx.load_module() directly to ensure proper tracking of the stacker and its contents.\
		The function will return a FlexStackerContext object that can be used as normal.
		
		:param self: TipTracker object
		:param slot: The deck slot to load the stacker module on
		:type slot: str
		:param tip_load_name: The API load name of the labware (tiprack) to load into the stacker
		:type tip_load_name: str
		:param initial_count: The initial number of tipracks to load into the stacker
		:type initial_count: int
		:param lid: The lid to load onto the tiprack in the stacker
		:type lid: str | None
		:param load_on_shuttle: Whether to load one tiprack onto the shuttle instead of storing it in the stacker
		:type load_on_shuttle: bool
		:return: The FlexStackerContext object representing the loaded stacker module
		:rtype: protocol_api.FlexStackerContext
		'''
		self._log(f'Adding stacker module on slot {slot} with {initial_count} {tip_load_name}')
		stacker_obj = self.ctx.load_module('flexStackerModuleV1', slot)
		if tip_load_name in self.stackers.keys():
			self.stackers[tip_load_name].append([stacker_obj,None,True if lid != None else False])
		else:
			self.stackers[tip_load_name] = [[stacker_obj,None,True if lid != None else False]]
		if not use_for_storing_empty:
			self.load_tips_in_stacker(stacker_obj,tip_load_name,initial_count,lid,load_on_shuttle)
		else:
			self._log(f'Using stacker on slot {slot} for storing empty tipracks, setting carousel to true')
			if self.carousel_tips == False:
				self.carousel_tips = True
				self.open_slot = stacker_obj
			if tip_load_name not in self.storing_stackers.keys():
				self.storing_stackers[tip_load_name] = [[stacker_obj,0]]
			else:
				self.storing_stackers[tip_load_name].append([stacker_obj,0])
		return stacker_obj


	def load_tips_in_stacker(self,stacker : protocol_api.FlexStackerContext,tip_load_name : str,quantity : int,lid : str | None = None, load_on_shuttle : bool = True) -> None:
		'''
		Function to load tipracks in stacker at the beginning of the protocol. Currently also used to reload stackers when they run out of tips, but will change this in the future.
		
		:param self: TipTracker object
		:param stacker: The FlexStackerContext to load tips into
		:type stacker: protocol_api.FlexStackerContext
		:param tip_load_name: The API load name of the labware (tiprack) to load into the stacker
		:type tip_load_name: str
		:param quantity: The number of tipracks to load into the stacker. With a max of 7. When quantity > 6, one tiprack will have to be loaded onto the shuttle.
		:type quantity: int
		:param lid: The lid to load onto the tiprack in the stacker. A lid will not be loaded onto the shuttle if load_on_shuttle is True.
		:type lid: str | None
		:param load_on_shuttle: Whether to load one tiprack onto the shuttle instead of storing it in the stacker. When quantity > 6, this must be True. For 6 and under, this can be set to False to store all tipracks in the stacker or set to true so that quantity - 1 tipracks are stored in the stacker and one on the shuttle.
		:type load_on_shuttle: bool
		:return: None
		:rtype: None
		'''
		self._log(f'Loading {quantity} {tip_load_name} into stacker in {stacker}')
		stacker.set_stored_labware(tip_load_name,count=quantity - 1 if load_on_shuttle else quantity,lid=lid)
		if tip_load_name not in self.tip_rack_counts.keys():
			self.tip_rack_counts[tip_load_name] = quantity
		else:
			self.tip_rack_counts[tip_load_name] = self.tip_rack_counts[tip_load_name] + quantity
		if load_on_shuttle:
			self._log(f'Loading labware onto stacker shuttle for {tip_load_name}')
			stacker.load_labware(tip_load_name)
		for x,stacker_list in enumerate(self.stackers[tip_load_name]):
			if stacker_list[0] == stacker:
				self.stackers[tip_load_name][x][1] = quantity - 1 if load_on_shuttle else quantity


	def _shuttle_labware(self,labware : protocol_api.Labware,location: str | protocol_api.ModuleContext | protocol_api.Labware) -> None:
		'''Internal function to move labware using the gripper or not based on settings. This is generally only used when moving labware from the stackers to the deck or when carouseling tipracks.'''
		self.ctx.move_labware(labware,location,use_gripper=self.use_gripper)
	
	def _refill_deck_manually(self,slots : list[str],tip_load_name : str) -> None:
		'''Internal function to refill tipracks manually by prompting the user to place new racks on the deck and then assigning them to the pipettes. This is used when not using the waste chute or gripper to move racks around, so the user has to manually move racks on and off the deck. This function will be called after prompting the user to remove old racks with clear_old() if not using the waste chute, and then will prompt the user to place new racks on the deck in the specified slots before assigning those slots to the given tip_load_name and assigning that tip_load_name to the pipettes.'''
		self._log(f'Please place new {tip_load_name} tipracks on deck in slots {slots}')
		self.ctx.pause(f'Please place new {tip_load_name} tipracks on deck in slots {slots}')
		self.reset_rack_list(tip_load_name)
		self.refill_tips(tip_load_name,slots,waste_all_old=False)
		self.reset_rack_list(tip_load_name)
	
	
	def _stacker_count_to_load(self, tip_load_name: str) -> int:
		'''Racks to request per stacker ``fill`` call (capped at 6 vs ``max_racks_count`` when set).'''
		max_c = self.max_racks_count.get(tip_load_name)
		if max_c is None:
			return 6
		return min(6, max_c - self.tip_rack_counts.get(tip_load_name, 0))

	def _stacker_operator_fill_modules(self, tip_load_name: str) -> None:
		'''For each Flex stacker holding ``tip_load_name``, update internal counts and call ``FlexStackerContext.fill`` (operator physically refills the module).'''
		self._log('No remaining tipracks in stackers, manual refill needed')
		count_to_load = self._stacker_count_to_load(tip_load_name)
		for x, stacker_row in enumerate(self.stackers[tip_load_name]):
			self.stackers[tip_load_name][x][1] = count_to_load
			self.stackers[tip_load_name][x][0].fill(count_to_load)

	def _stacker_deploy_last_racks_when_capped(self, tip_load_name: str, deposit_targets: list) -> None:
		'''When ``max_racks_count`` is reached, skip further deck pauses and shuttle the last racks from stackers onto ``deposit_targets`` (same objects as ``pick_up`` passes for empty racks).'''
		if self.max_racks_count.get(tip_load_name, None) != self.tip_rack_counts.get(tip_load_name, -1):
			return
		count_to_load = self._stacker_count_to_load(tip_load_name)
		self.call_refill = False
		self._log(f'Max racks of {tip_load_name} reached, last racks in stacker')
		for empty_slot in deposit_targets[:count_to_load]:
			next_rack = self.move_from_stacker(tip_load_name)
			self._shuttle_labware(next_rack, self._shuttle_target_for_stacker_place(empty_slot))

	def refill_stacker_supply(self, tip_load_name: str, *, deposit_targets: list | None = None) -> None:
		'''
		Operator refill for **all** Flex stacker modules registered under ``tip_load_name``: runs ``fill`` on each (see Opentrons Flex Stacker docs — ``fill`` guides the user to load labware into the module).

		When ``max_racks_count`` equals the number of racks already consumed for this type, the tracker instead shuttles the last racks from the stacker(s) onto ``deposit_targets`` (typically empty rack positions from ``pick_up``) and sets ``call_refill`` False so the deck pause can be skipped.

		:param deposit_targets: Optional list of deck locations (slot strings or labware) matching the legacy ``empty_tipracks`` argument to the internal stacker refill; defaults to an empty list when calling outside ``pick_up``.
		'''
		if tip_load_name not in self.stackers:
			raise ValueError(f'No stackers registered for {tip_load_name}; use add_stacker() first.')
		targets = [] if deposit_targets is None else deposit_targets
		self._stacker_operator_fill_modules(tip_load_name)
		self._stacker_deploy_last_racks_when_capped(tip_load_name, targets)

	def reload_stacker_inventory(
		self,
		tip_load_name: str,
		quantity: int,
		lid: str | None = None,
		load_on_shuttle: bool = True,
	) -> None:
		'''
		For each stacker module that holds ``tip_load_name``, pause so the operator can load hardware, then call ``load_tips_in_stacker`` (``set_stored_labware`` / shuttle ``load_labware``) to match simulator state.

		Use for proactive restocking without waiting for ``retrieve`` to fail. ``quantity`` is per module (same cap as ``load_tips_in_stacker``, typically up to 7 with lids). Pass ``lid`` when stackers were configured with lids for that tip type.
		'''
		if tip_load_name not in self.stackers:
			raise ValueError(f'No stackers registered for {tip_load_name}; use add_stacker() first.')
		for stacker_row in self.stackers[tip_load_name]:
			stacker_mod = stacker_row[0]
			use_lid = lid if stacker_row[2] else None
			self._log(f'Prepare stacker {stacker_mod} with {quantity} × {tip_load_name}')
			self.ctx.pause(
				f'Load {quantity} × {tip_load_name} into the Flex stacker ({stacker_mod}), then resume.'
			)
			self.load_tips_in_stacker(stacker_mod, tip_load_name, quantity, use_lid, load_on_shuttle)

	def _refill_stacker_manually(self,tip_load_name : str, empty_tipracks : list[str] = []) -> None:
		'''
		Internal shim: same as ``refill_stacker_supply`` for legacy call sites.
		'''
		self.refill_stacker_supply(tip_load_name, deposit_targets=empty_tipracks)
	
	
	def grab_from_stacker(self,tip_load_name : str, empty_slots : list[str] = []) -> None:
		'''
		Grab the next tiprack available from the stacker for the given rack type. If a tiprack has a lid, it will be removed and placed in the waste bin. If the stacker has the proper racktype on the shuttle, it will return the shuttle labware. If the stacker has a different labware on it, it will move it to the open_slot first. \
		Moves labware onto the deck; does not return the labware object to the caller.
		
		:param self: TipTracker object
		:param tip_load_name: The API load name of the labware (tiprack) to retrieve from the stacker
		:type tip_load_name: str
		:return: None
		:rtype: None
		'''
		if not self.carousel_tips:
			for slot in empty_slots:
				for stacker in self.stackers[tip_load_name]:
					if stacker[1] > 0:
						self._log(f'Tiprack in {stacker[0]}, moving to {slot}')
						next_rack = self.move_from_stacker(tip_load_name)
						self._shuttle_labware(next_rack, self._shuttle_target_for_stacker_place(slot))
						break
					else:
						self._log(f'No remaining tipracks in {stacker[0]}')
		else:
			self._log('Tiprack in stacker, carouseling to active deck')
			for old_rack in empty_slots:
				for stacker in self.stackers[tip_load_name]:
					if stacker[1] > 0:
						next_rack = self.move_from_stacker(tip_load_name)
						self.carousel(old_rack,next_rack)
						break
					else:
						self._log(f'No remaining tipracks in {stacker[0]}')

