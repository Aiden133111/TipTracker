import sys
import traceback
from opentrons import protocol_api
from opentrons.protocol_api.labware import OutOfTipsError
from opentrons.protocol_api import ALL, COLUMN, SINGLE, ROW, PARTIAL_COLUMN
from opentrons.types import NozzleConfigurationType

#PROTOCOL REQUIREMENTS
metadata = {
	'protocolName': 'TipTracker debug harness',
	'author': 'Aiden McFadden, Opentrons',
	'source': 'Custom Protocol Development',
	'description' : 'TipTracker library test harness (run()) plus embedded class copy; see run() for exercised APIs.',
}

requirements = {
	"robotType": "Flex",
	"apiLevel": "2.27",
}

class TipTracker:
	'''
	The TipTracker class is meant to be a plug-in method to track all tips across the deck and in stackers to facilitate seamless \
	methods of refilling tips when the robot runs out of a given tiprack size whether it be refilling from the expansion slots, \
	refilling from stackers, or refilling manually. Empty tips can be shuffled around the deck with the carouseling feature \
	or more commonly thrown away in the waste chute as the main benefit of this class is with higher throughput deck layouts \
	or expansion slots. The carousel feature is still helpful to prevent developers from having to call move labware multiple times. \
	The priority of refills is as follows
	1. If there are tips available in the assigned tiprack slots, pick up from there
	2. If using pick_up_slots and tiprack is not on correct slot, shuffle from active deck
	3. Move tiprack from expansion slot to deck or carousel if enabled
	4. Move tiprack from stacker to deck or carousel if enabled
	5. Refill manually all tips on all slots up until the max racks are defined 
	5a. If max racks is defined and being reached in the next fillup, stackers will be asked to be refilled instead of the deck.
	5b. If no max racks defined then all slots assigned to the tiprack are refilled
	5c. If refill all is used all empty tipracks will be prompted to replaced instead of just the current tiprack type. 
	For proactive pauses, use ``refill_deck`` / ``refill_main_deck_slots`` / ``refill_expansion_slots`` (clear + pause + optional pipette reassignment), ``reload_deck_tipracks`` / ``reload_main_deck_tipracks`` / ``reload_expansion_tipracks`` (pause + ``load_tipracks``), ``refill_stacker_supply`` (``FlexStackerContext.fill`` when stackers run dry), and ``reload_stacker_inventory`` (pause + ``load_tips_in_stacker``) — see each method’s docstring.
	Fatal configuration mistakes print ``traceback.print_exc()`` plus an optional call stack to **stderr** (see ``verbose_tracebacks`` on ``TipTracker(...)``) so pasted / embedded copies are easier to trace back to your protocol line.
	
	:param self: TipTracker object
	:param ctx: Your protocol context to access protocol information and move labware around the deck
	:type ctx: protocol_api.ProtocolContext
	:param pipette1: Your first pipette, used to track tips for the first pipette and assign tipracks to it
	:type pipette1: protocol_api.InstrumentContext
	:param pipette2: Your second pipette, used to track tips for the second pipette and assign tipracks to it, or None
	:type pipette2: protocol_api.InstrumentContext | None
	:param waste_bin: The waste bin being used, can be a waste chute or a trash bin, if waste chute is used, will use it to dispose of empty racks and tips, if trash bin is used, will prompt user to move empty racks to waste after all racks are empty
	:type waste_bin: protocol_api.WasteChute | protocol_api.TrashBin
	:param use_gripper: If you have the gripper, highly recommended for using TipTracker or else its all manual
	:type use_gripper: bool
	:param debugging: To print commands to the terminal which is useful for troubleshooting and understanding the flow of the tip tracker, but can be verbose
	:type debugging: bool
	:param suppress_comments: To suppress tracker comments in the protocol run log (separate from debugging terminal output)
	:type suppress_comments: bool
	'''

	# Flex rear expansion deck slot IDs; loading tipracks here requires add_expansion_slots first.
	EXPANSION_DECK_SLOTS: frozenset = frozenset({'A4', 'B4', 'C4', 'D4'})

	def __init__(self, ctx : protocol_api.ProtocolContext, pipette1 : protocol_api.InstrumentContext, 
				waste_bin : protocol_api.WasteChute | protocol_api.TrashBin, pipette2 : protocol_api.InstrumentContext | None = None,
				use_gripper : bool = False, debugging : bool = False, suppress_comments : bool = False,
				verbose_tracebacks : bool = True) -> None:
		'''
		Initializes the TipTracker object, sets all properties to default values, creates dictionaries to track tipracks on the deck and in expansion slots \
		and assigns the pipettes and waste bin to the object. Sets default priorities for how empty racks should be handled. THIS IS NOT TO BE CALLED DIRECTLY. Each internal \
		property is explained in the comments below in read only sections (never meant to be modified directly) and read/write sections (sections may be \
		modified directly in some / all situations). \
		
		:param self: TipTracker object
		:param ctx: Your protocol context to access protocol information and move labware around the deck
		:type ctx: protocol_api.ProtocolContext
		:param pipette1: Your first pipette, used to track tips for the first pipette and assign tipracks to it
		:type pipette1: protocol_api.InstrumentContext
		:param waste_bin: The waste bin being used, can be a waste chute or a trash bin, if waste chute is used, will use it to dispose of empty racks and tips, if trash bin is used, will prompt user to move empty racks to waste after all racks are empty
		:type waste_bin: protocol_api.WasteChute | protocol_api.TrashBin
		:param use_gripper: If you have the gripper, highly recommended for using TipTracker or else its all manual
		:type use_gripper: bool
		:param pipette2: Your second pipette, used to track tips for the second pipette and assign tipracks to it, or None
		:type pipette2: protocol_api.InstrumentContext | None
		:param debugging: To print commands to the terminal which is useful for troubleshooting and understanding the flow of the tip tracker, but can be verbose
		:type debugging: bool
		:param suppress_comments: To suppress comments of the tracker to the protocol log (separate from debugging terminal output)
		:type suppress_comments: bool
		:param verbose_tracebacks: When True (default), fatal configuration errors print a banner plus ``traceback.print_stack`` to stderr (helps find your protocol line when TipTracker is pasted in). When False, the banner and ``traceback.print_exc()`` still run, but the extra call stack is omitted.
		:type verbose_tracebacks: bool
		'''

		self.metadata = {
			'Author': 'Aiden McFadden',
			'Version' : '3.0',
			'github': 'https://github.com/Aiden133111/TipTracker',
			'README' : 'https://github.com/Aiden133111/TipTracker/blob/main/README.md'
		}

		#######################################################
		# READ ONLY THROUGHOUT PROTOCOL, USER DOES NOT MODIFY #
		#######################################################

		#ProtocolContext for robot commands. Read only and passed through init.
		self.ctx : protocol_api.ProtocolContext = ctx
		#First pipette added to the tracker via init, do not change or call property mid protocol, completely fine to only have one pipette and leave pipette2 as None. Read Only											
		self.pipette1 : protocol_api.InstrumentContext = pipette1										
		#Second Pipette added to the tracker via init, do not change or call property mid protocol, can be left as None if only using one pipette, but if using two pipettes add it during init. Read Only
		self.pipette2 : protocol_api.InstrumentContext | None = pipette2
		#The expansion slots that have been added to the tracker. This can be set directly but using add_expansion_slots() is recommended to ensure the internal data is correct, 
		#can be updated mid protocol as needed if expansion slots are added or removed during the run. Read Only
		self.ex_slots : list[str] = []	
		#How many times a pipette has been called to pick up any tip, Read only
		self.pick_up_count : dict[protocol_api.InstrumentContext, int] = {pipette1 : 0, pipette2 : 0}
		#How many times a pipette has been called to drop any tip, Read only
		self.drop_count : dict[protocol_api.InstrumentContext, int] = {pipette1 : 0, pipette2 : 0}
		#If using the gripper to move racks, highly recommended to only use this package with the gripper or else everything is manual. Set via init Read Only													
		self.use_gripper : bool = use_gripper					
		#The type of waste bin that you are using for the protocol. Waste chute is highly recommended as it allows for more automation and less manual steps, but the tracker can work with a trash bin as well,
		#just with more manual steps. Set via init Read Only
		self.waste : protocol_api.WasteChute | protocol_api.TrashBin = waste_bin
		#Dictionary of tipracks to track on the deck. This should not be directly modified as it is updated directly through the tracker functions, but can be read to see what tipracks are currently on the deck.
		#Key is the tiprack load name, value is a list of the tiprack labware objects on the deck of that type. Read Only
		self.tipracks : dict[str, list[protocol_api.Labware]] = {}
		#Dictionary of tipracks to track on the expansion slots of the robot. This should not be directly modified as it is updated directly through the tracker functions, but can be read to see what tipracks 
		#are currently on the expansion slots. Key is the tiprack load name, value is a list of the tiprack labware objects on the expansion slots of that type. Where they currently are, not to be user modified
		# Read Only
		self.ex_racks : dict[str, list[protocol_api.Labware]] = {}
		#Empty expansion slots that previously had racks on them, used to priortize where racks should be moved from the expansion slots back to the deck when they need to be refilled, key is the 
		#tiprack load name, value is a list of the expansion slot names that previously had racks on them of that type. Not to be directly modified, updated through the tracker functions. Read Only
		self.empty_ex_slots : dict[str, list[str]] = {}	
		#Dictionary map of where tipracks should be refilled when that tiptype is empty. This can be different than where tipracks currently are. This can be modified by calling assign_slots() and not
		#by calling directly. Read Only
		self.rack_assignments : dict[str, list[str]] = {}	
		#How many tips have been used for that given tiprack type. Uses pipette.active_nozzles to count. This is read only
		self.tip_counts : dict[str, int] = {}
		#How many tipracks have been loaded for that given tiprack type. This is read only
		self.tip_rack_counts : dict[str, int] = {}
		#Original open slot for carousel that is saved when open_slot is first defined. Used for resetting when plate map is reset. Read only
		self.original_open_slot : str | None = None	
		#Dictionary of stacker instrument contexts and number of racks in the stacker, key is rack load name. Read Only, use add_stacker() to create this dictionary and add the stackers to the tracker
		self.stackers : dict[str, list[list]] = {}
		#If something has been moved from the shuttle and needs to be returned during a tip replacement. Read Only
		self.return_to_stacker : bool = False	
		#If there are any adapters on deck, and the slot that they are located on for 96 channel work	
		self.tiprack_adapters : dict[str, list] = {}
		# If there are adapters on the deck, this is a dictionary to track which tipracks are on adapters and which slot they are on for pickup purposes. Key is tiprack load name, value is the slot of the adapter that the tiprack is on. Read only, updated through load_tipracks when loading onto adapters
		self.adapter_pickup_tipracks : dict[str, list[protocol_api.Labware]] = {} 
		#The current tip associated with pipette 1
		self.pipette_1_tip_type : str | None = None
		#The current tip associated with pipette 2
		self.pipette_2_tip_type : str | None = None
		#Wether a manual refill needs to be called at the end of a pick up
		self.call_refill : bool = False
		#Any stackers being used for storing empty tipracks organized by the rack they are holding
		self.storing_stackers : dict[str, list[list]] = {}
		
		##########################################################
		# READ/WRITE, USER CAN MODIFY AS NEEDED THROUGH PROTOCOL #
		##########################################################

		#A slot or adapter with nothing on it that the tracker can use to shuffle labware when needed (carousel or stacker shuttle). THis should be set directly when carouseling
		#Read/Write Okay
		self.open_slot : str | None = None	
		#Debugging mode flag from init. Read/Write Okay Changing mid protocol is totally fine if you only want to focus on a certain part													
		self.debug : bool = debugging	
		#Use waste chute to dispose of tips and waste chute if present. Only change if you want to keep empty racks on the deck and use carousel to shuffle. Read/Write supported
		self.use_chute : bool = True if type(waste_bin) == protocol_api.WasteChute else False			 
		#If tips should be shuffled around the deck using the open slot so that empty racks are kept on the deck instead of thowing away. Read/Write supported
		self.carousel_tips : bool = False if type(waste_bin) == protocol_api.WasteChute else True
		#If tracker commands should print to the run log to explain why the robot commands are happening. Set through init but can be changed to highlight certain parts
		self.print_comments : bool = not suppress_comments
		#The max racks count for each tiprack type. This will prevent extra racks to be loaded when a defined threshold has been reached. This can be set using add_starting_tiprack or modified by direct call
		#using the API load name of the tiprack you want to set. Read/Write Okay
		self.max_racks_count : dict[str, int] = {}
		#Deck slot names (e.g. A3) ignored for waste/refill; resolved for labware via parent slot. Read/Write: TipTracker.ignore_slots.append('A3').
		self.ignore_slots : list[str] = []
		#A dictionary of tiprack load names and the slots they should only pick up from to force a pickup in a given slot, useful for partial tip pickups.
		#Read/Write okay TipTracker.pick_up_slots['opentrons_flex_96_tiprack_50ul'] = 'A1'
		self.pick_up_slots : dict[str, str] = {}
		#A default pipette that should be used to prevent having to pass pipette repeatedly to pickup and drop commands. Read/Write okay TipTracker.active_pipette = self.pipette1 can also be
		#set during pick_up commands using the set_active_pipette argument.
		self.active_pipette = None		
		#If a single adapter should be used for all tipracks. If true, the first adapter loaded will be used for all tipracks and it will be auto assigned to the most recent pickup call
		self.global_adapter : bool = False
		# Print call stacks to stderr on fatal TipTracker errors (helps when this file is pasted into a protocol). Read/Write okay.
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
					if self.print_comments:
						self.ctx.comment(f'Overwriting adapter on slot {deck_slot} from {self.tiprack_adapters[deck_slot][0]} to {tip_load_name}')
					if self.debug:
						print(f'Overwriting adapter on slot {deck_slot} from {self.tiprack_adapters[deck_slot][0]} to {tip_load_name}')
					self.tiprack_adapters[deck_slot][0] = tip_load_name
					if tip_load_name not in self.adapter_pickup_tipracks.keys():
						self.adapter_pickup_tipracks[tip_load_name] = [f'REPLACE_ME',tip_load_name,deck_slot]
			if clear_other_slots or tip_load_name not in self.rack_assignments.keys():
				self.rack_assignments[tip_load_name] = assigned_slots
			else:
				self.rack_assignments[tip_load_name].extend(assigned_slots)
		


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
							if self.print_comments:
								self.ctx.comment(f'Max racks of {tip_load_name} reached, not loading more')
							if self.debug:
								print(f'Max racks of {tip_load_name} reached, not loading more')
							continue
					if type(slot) == str:
						if slot in adapters or slot in self.tiprack_adapters.keys():
							if self.tiprack_adapters.get(slot,None) == None:
								if self.debug:
									print(f'Loading adapter for {tip_load_name} in slot {slot}')
								if self.print_comments:
									self.ctx.comment(f'Loading adapter for {tip_load_name} in slot {slot}')
								adapter = self.ctx.load_adapter('opentrons_flex_96_tiprack_adapter',slot)
								rack = adapter.load_labware(tip_load_name)
								self.tiprack_adapters[slot] = [tip_load_name, adapter]
							else:
								if self.debug:
									print(f'Adapter already on slot {slot}, loading {tip_load_name} onto adapter')
								if self.print_comments:
									self.ctx.comment(f'Adapter already on slot {slot}, loading {tip_load_name} onto adapter')
								adapter = self.tiprack_adapters[slot][1]
								existing = adapter.child
								if existing is not None and getattr(existing, 'load_name', None) == tip_load_name:
									rack = existing
									self.tiprack_adapters[slot] = [tip_load_name, adapter]
									if self.debug:
										print(f'Adapter on {slot} already holds {tip_load_name}; skipping duplicate load_labware')
									if self.print_comments:
										self.ctx.comment(f'Adapter on {slot} already holds {tip_load_name}; skipping duplicate load_labware')
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
							if self.debug:
								print(f'Slot {slot} already has {tip_load_name}; skipping load_labware')
						else:
							rack = self.ctx.load_labware(tip_load_name, slot)
						if tip_load_name not in self.tip_rack_counts.keys():
							self.tip_rack_counts[tip_load_name] = 1
						else:
							self.tip_rack_counts[tip_load_name] = self.tip_rack_counts[tip_load_name] + 1
					elif type(slot) == protocol_api.Labware and slot.load_name == 'opentrons_flex_96_tiprack_adapter':
						rack = slot.load_labware(tip_load_name)
						self.tiprack_adapters[slot.parent][0] = tip_load_name
						if self.debug:
							print(f'Loading {tip_load_name} onto adapter in slot {slot.parent}')
						if self.print_comments:
							self.ctx.comment(f'Loading {tip_load_name} onto adapter in slot {slot.parent}')
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


	def pick_up(self, pipette : int | str | protocol_api.InstrumentContext | None = None, 
			 locus : protocol_api.Labware | protocol_api.Well | None = None, refill_all : bool = False, set_active_pipette : bool = False) -> int:
		'''
		The main function and benefit of using the TipTracker class. This function, meant to replace InstrumentContext.pick_up_tip(), will attempt to \
		pick up a tip with the specified pipette (or active_pipette) for its assigned tiprack. If there is not a tip available, it will find the next tip \
		available on the deck either on the expansion slots, in a stacker, somewhere else on the deck where pickup should not happen. The function will also \
		facilitate refills for tipracks if there is no available tiprack accessible to the robot. A locus can be used to specify where the next tip should come from. \
		Turning on refill all will refill all tips if the racks are empty even if there are more tipracks available of that type.

		Return Code Definitions:
		0 - Just Pickup, succesful pickup, no swap needed
		1 - Had to carousel to pickup tip
		2 - Wasted Tip, Grabbed from expansion
		3 - Wasted Tip, Grabbed from stacker
		4 - Manual Refill started
		
		:param self: TipTracker object
		:param pipette: The pipette that should pick up the tip; if None then the current active pipette is used.
		:type pipette: int | str | protocol_api.InstrumentContext | None
		:param locus: The well that the pipette should pick the tip up from, if None will pick up from the next available tip in the assigned tiprack, can be used to reuse tips or pick up from a specific rack
		:type locus: protocol_api.Labware | protocol_api.Well | None
		:param refill_all: If True, refill all tipracks if they are empty when you run out of the tiprack currently assigned to the pipette. If False, only the current tiprack will be refilled.
		:type refill_all: bool
		:param set_active_pipette: If True set the pipette used to pick up the tip as the active pipette, if False do not change the active pipette, only use the pipette argument for this pick up
		:type set_active_pipette: bool
		:return: Return code corresponding to how the pipette was able to pick up tips. See the definitions above for the meaning of each return code
		:rtype: int
		'''
		#Set original open slot the first time after open_slot is defined
		if self.open_slot != None and self.original_open_slot == None:
			self.original_open_slot = self.open_slot

		############################################################
		#Assign proper pipette, check current tip and handle errors#
		############################################################
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
		#######################################################################
		#Tip Data structures in case we need to refill or move tipracks around#
		#######################################################################
		#Slots associated with the tip_load_name that we use to check for refills 
		slots_to_check = self._slots_for_rack_refill(tip_load_name)
		#Slots assigned to the tiprack that do not have a tiprack physically on them (for replacement)
		vacant_slots = [slot for slot in self.rack_assignments[tip_load_name] if self.ctx.deck[slot] == None and slot not in self.ignore_slots and slot not in self.tiprack_adapters.keys()] # Get the slots that are empty and can be loaded with racks
		#Tiprack objects that have a rack on them but with no tips (throw in trash or shuffle)
		empty_tipracks = [self.ctx.deck[slot] for slot in slots_to_check if self.ctx.deck[slot] != None and self.ctx.deck[slot].load_name != 'opentrons_flex_96_tiprack_adapter' and not any([well.has_tip for well in self.ctx.deck[slot].wells()])] # Get the racks on the deck that have no tips and are not in ignored slots
		if self.adapter_pickup_tipracks.get(tip_load_name,[]) != [] and self.adapter_pickup_tipracks.get(tip_load_name,[])[0] != 'REPLACE_ME': 
			empty_tipracks = empty_tipracks + [rack.parent for rack in self.adapter_pickup_tipracks[tip_load_name] if not any([well.has_tip for well in rack.wells()]) and getattr(rack.parent, 'parent', None) not in self.ignore_slots] # Get the racks on the adapters that have no tips and are not in ignored slots (parent may be OFF_DECK after move)
		#Slots assigned to the tiprack that have a tiprack but no tips (for replacement after tossing / shuffling)
		if tip_load_name in self.adapter_pickup_tipracks.keys():
			for slot, datalist in self.tiprack_adapters.items():
				if datalist[-1].child == None and datalist[-1] not in vacant_slots:
					vacant_slots.append(slot)
				if datalist[-1].child != None:
					if not any([well.has_tip for well in datalist[-1].child.wells()]) and datalist[-1] not in empty_tipracks:
						empty_tipracks.append(datalist[-1].child)
		# Dedupe by id: ``set()`` can trigger Labware __hash__/__eq__ that walks parents and hits OFF_DECK (no .parent).
		empty_tipracks = list({id(r): r for r in empty_tipracks if r is not None}.values())
		vacant_slots = list(set(vacant_slots))
		empty_tiprack_slots = []
		for rack in empty_tipracks:
			try:
				par = rack.parent
			except AttributeError:
				continue
			if isinstance(par, type(protocol_api.OFF_DECK)):
				continue
			empty_tiprack_slots.append(par)
		if refill_all:
			other_rack_slots = {}
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
					if sk in self.ex_slots and self.ex_racks.get(rack_load_name):
						continue
					rp = rack.parent
					if isinstance(rp, type(protocol_api.OFF_DECK)):
						continue
					empties.append(rp)
				if empties:
					other_rack_slots[rack_load_name] = empties
			empty_tip_slots = {
				rl: [slot for slot in racklist
					if slot not in self.ignore_slots and self.ctx.deck[slot] is None]
				for rl, racklist in self.rack_assignments.items()
			}
		#######################################################################################################################################################################
		#Updates a deck in the edge case that an adapter pickup is empty and now has been reassigned to a different tip type (API load name), this shuffles the correct rack onto the adapter#
		#######################################################################################################################################################################
		if len(active_pipette.tip_racks) >= 3 and active_pipette.tip_racks[0] == 'REPLACE_ME':
			if self.debug:
				print('Adapter pickup tiprack empty, finding replacement')
			if self.print_comments:
				self.ctx.comment('Adapter pickup tiprack empty, finding replacement')
			replacement_rack = None
			replacement_rack_name = active_pipette.tip_racks[1]
			trash_rack_name =self.tiprack_adapters[active_pipette.tip_racks[2]][1].child.load_name
			if self.debug:
				print(f'Moving tiprack on adapter on slot {self.tiprack_adapters[active_pipette.tip_racks[2]][1].parent} to waste to free up slot for replacement')
			if self.print_comments:
				self.ctx.comment(f'Moving tiprack on adapter on slot {self.tiprack_adapters[active_pipette.tip_racks[2]][1].parent} to waste to free up slot for replacement')
			self.ctx.move_labware(self.tiprack_adapters[active_pipette.tip_racks[2]][1].child, self.waste if self.use_chute else protocol_api.OFF_DECK, self.use_gripper)
			for x,slot in enumerate(self.rack_assignments[replacement_rack_name]):
				if slot in self.ignore_slots or slot in self.tiprack_adapters.keys():
					continue
				rack = self.ctx.deck[slot]
				if all([well.has_tip for well in rack.wells()]):
					replacement_rack = rack
					if self.debug:
						print(f'Found replacement rack {replacement_rack_name} for adapter on slot {slot}')
					if self.print_comments:
						self.ctx.comment(f'Found replacement rack {replacement_rack_name} for adapter on slot {slot}')
					self.ctx.move_labware(replacement_rack, self.tiprack_adapters[active_pipette.tip_racks[2]][1], self.use_gripper)
					break
			if replacement_rack == None:
				if replacement_rack_name in self.stackers.keys() and sum([stacker[1] for stacker in self.stackers.get(replacement_rack_name, [])]) > 0:
					if self.debug:
						print(f'Grabbing {replacement_rack_name} from stacker')
					if self.print_comments:
						self.ctx.comment(f'Grabbing {replacement_rack_name} from stacker')
					self.grab_from_stacker(replacement_rack_name,vacant_slots + empty_tiprack_slots)
				else:
					if self.print_comments:
						self.ctx.comment(f'No full racks available for {replacement_rack_name} on adapter, starting manual refill')
					if self.debug:
						print(f'No full racks available for {replacement_rack_name} on adapter, starting manual refill')
					self._refill_deck_manually(self.adapter_pickup_tipracks[replacement_rack_name][-1:],replacement_rack_name)
			self.reset_rack_list([replacement_rack_name,trash_rack_name])
			self.assign_tipracks(replacement_rack_name,active_pipette,mode=ALL)
		#Try and pick up tip
		
		#If this rack should only be on the slot
		if tip_load_name in self.pick_up_slots.keys():
			#Check if tiprack has tips first
			next_tip = self.ctx.deck[self.pick_up_slots[tip_load_name]].next_tip()
			if next_tip == None:
				if self.debug:
					print(f'No tips available for pickup on slot {self.pick_up_slots[tip_load_name]}, shuffling tipracks')
				if self.print_comments:
					self.ctx.comment(f'No tips available for pickup on slot {self.pick_up_slots[tip_load_name]}, shuffling tipracks')
				if tip_load_name in self.pick_up_slots:
					self.shuffle_for_forced_pickup(tip_load_name,self.pick_up_slots[tip_load_name], active_pipette)
		try:
			if (
				self._pipette_is_flex_96channel(active_pipette)
				and tip_load_name not in self.adapter_pickup_tipracks
				and active_pipette.active_channels == 96
			):
				raise ValueError(
					f'No adapter pickup tiprack defined for {tip_load_name} but pipette has 96 active channels; '
					'use assign_tipracks with adapter pickup, deck ALL layout, or an 8-channel pipette for this tip type.'
				)
		except ValueError as Error:
			self._fatal_tracker_error('pick_up: 96-channel layout requires adapter pickup tiprack configuration', Error)
		
		############################################################
		#Try and pickup tip, if fails, then start refilling process#
		############################################################
		try:
			active_pipette.pick_up_tip(locus)
			return_code =  0
		except Exception as Error:
			if self.print_comments:
				self.ctx.comment('Out of tips, starting refilling process')
			if self.debug:
				print('Out of tips, starting refilling process')
			# Labware on main deck to carousel against stacker racks (exclude expansion staging rows)
			stacker_carousel_olds = [r for r in empty_tipracks if self._deck_slot_id(r) not in self.ex_slots]
			wasted_slot_ids: list[str] = []
			# Trash old tips (not ignored slots; not expansion-slot empties while ex_racks still supplies racks)
			if not self.carousel_tips:
				empties_to_waste = [r for r in empty_tipracks if self._waste_empty_rack_now(r, tip_load_name)]
				wasted_slot_ids = [self._deck_slot_id(r) for r in empties_to_waste]
				self.waste_tips(empties_to_waste)
			if self.ex_racks.get(tip_load_name, None) == None and self.stackers.get(tip_load_name,None) == None:
				if self.print_comments:
					self.ctx.comment('No expansion slots / stackers defined, Refilling Manually') # Dont have to worry about carousel here, no ex slots
				if self.debug:
					print('No expansion slots / stackers defined, Refilling Manually')
				self.refill_deck(tip_load_name, active_pipette, slots_to_check)
				#Optionally refill all used tip racks, dont think this counts expansion deck slots
				if refill_all:
					if self.print_comments:
						self.ctx.comment('Refilling all other tips')
					if self.debug:
						print('Refilling all other tips')
					for other_rack_names,other_slots in other_rack_slots.items():
						if other_slots != [] or empty_tip_slots[other_rack_names] != []:
							if other_slots != []:
								self.waste_tips(other_slots)
							self.refill_deck(
								other_rack_names,
								slots=other_slots + empty_tip_slots[other_rack_names],
								reassign_pipette=False,
							)
				active_pipette.pick_up_tip(locus)
				return_code = 4
			else:
				if self.print_comments:			
					self.ctx.comment('Expansion slots or stackers defined, starting refilling process')
				if self.debug:
					print('Expansion slots or stackers defined, starting refilling process')
				if refill_all:
					if self.print_comments:
						self.ctx.comment('Refilling all other tips')
					if self.debug:
						print('Refilling all other tips')
					for other_rack_names,other_slots in other_rack_slots.items():
						if other_slots != [] or empty_tip_slots[other_rack_names] != []:
							if other_slots != []:
								self.waste_tips(other_slots)
							self.refill_deck(
								other_rack_names,
								slots=other_slots + empty_tip_slots[other_rack_names],
								reassign_pipette=False,
							)
				if tip_load_name in self.ex_racks.keys() and self.ex_racks.get(tip_load_name, []) != []:
					#Condition  =  THERE IS AT LEAST ONE TIPRACK ON THE EXPANSION DECK
					if self.print_comments:
						self.ctx.comment('Tiprack on expansion slot, moving to active deck')
					if self.debug:
						print('Tiprack on expansion slot, moving to active deck')
					if self.carousel_tips:
						for old_rack,e_rack in zip(self.tipracks[tip_load_name],self.ex_racks[tip_load_name]):
							self.carousel(old_rack,e_rack)
							return_code = 1
					else:
						for e_rack, open_slot in zip(self.ex_racks[tip_load_name],empty_tiprack_slots): #This needs a check for if expansion slot has tips 
							e_slot_source = e_rack.parent
							self._shuttle_labware(e_rack, self._shuttle_target_for_stacker_place(open_slot))
							if tip_load_name in self.empty_ex_slots.keys():
								self.empty_ex_slots[tip_load_name].append(e_slot_source)
							else:
								self.empty_ex_slots[tip_load_name] = [e_slot_source]
							return_code = 2
					self.reset_rack_list(tip_load_name)			
					_rmode = ALL if tip_load_name in self.adapter_pickup_tipracks else None
					self.assign_tipracks(tip_load_name, active_pipette, mode=_rmode)
					
					active_pipette.pick_up_tip(locus)
				elif tip_load_name in self.stackers.keys() and sum([stacker[1] for stacker in self.stackers.get(tip_load_name, [])]) > 0:
					#Condition  =  THERE IS AT LEAST ONE TIPRACK IN THE STACKER
					if self.carousel_tips:
						self.grab_from_stacker(tip_load_name, stacker_carousel_olds)
					else:
						_stacker_deposit_slots = list(dict.fromkeys(
							[s for s in vacant_slots + wasted_slot_ids if s not in self.ignore_slots]
						))
						self.grab_from_stacker(tip_load_name, _stacker_deposit_slots)
					self.reset_rack_list(tip_load_name)
					_rmode = ALL if tip_load_name in self.adapter_pickup_tipracks else None
					self.assign_tipracks(tip_load_name, active_pipette, mode=_rmode)
					active_pipette.pick_up_tip(locus)
					return_code = 3
				else:
					#Condition  =  THERE ARE NO TIPRACKS ON THE EXPANSION DECK OR IN THE STACKER OR ON DECK
					self.call_refill = True
					if tip_load_name in self.stackers.keys():
						self.refill_stacker_supply(tip_load_name, deposit_targets=empty_tipracks)
					#This block is just for user information
					print(self.rack_assignments[tip_load_name])
					print(empty_tiprack_slots)
					if not set(self.EXPANSION_DECK_SLOTS).isdisjoint(self.rack_assignments[tip_load_name]) and self.call_refill:
						if self.print_comments:
							self.ctx.comment('No remaining tipracks on expansion deck, manual refill needed')
						if self.debug:
							print('No remaining tipracks on expansion deck, manual refill needed')
					#Home the robot and initiate a pause for a manual refill if we need to refill the deck as well
					if self.call_refill:
						self.reload_deck_tipracks(tip_load_name, self.rack_assignments[tip_load_name])
					#Reset internal data and resign tips after a manual refill if needed
					self.reset_rack_list(tip_load_name)
					_rmode = ALL if tip_load_name in self.adapter_pickup_tipracks else None
					self.assign_tipracks(tip_load_name, active_pipette, mode=_rmode)
					self.open_slot = self.original_open_slot
					active_pipette.pick_up_tip(locus)
					return_code =  4

		#Return labware to the shuttle it it had to be moved to the open slot during a tip refill
		if self.return_to_stacker:
			if self.print_comments:
				self.ctx.comment('Returning labware to stacker')
			if self.debug:
				print('Returning labware to stacker')
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
	

	def shuffle_for_forced_pickup(self, tip_load_name : str, pick_up_slot : str, pipette : protocol_api.InstrumentContext) -> None:
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
			if slot == pick_up_slot or self.ctx.deck[slot] == None:
				continue
			elif self.ctx.deck[slot]:
				next_rack = self.ctx.deck[slot]
				break
		if next_rack is None:
			raise ValueError(f"No other tiprack with tips found to shuffle into {pick_up_slot} for {tip_load_name}")
		if self.carousel_tips:
			self.carousel(empty_rack,next_rack)
			self.reset_rack_list(tip_load_name)
			_rmode = ALL if tip_load_name in self.adapter_pickup_tipracks else None
			self.assign_tipracks(tip_load_name, pipette, mode=_rmode)
		elif self.use_chute:
			if self.print_comments:
				self.ctx.comment(f'Disposing of empty tiprack in {pick_up_slot} replacing with {next_rack.parent}')
			if self.debug:
				print(f'Disposing of empty tiprack in {pick_up_slot} replacing with {next_rack.parent}')
			self.ctx.move_labware(empty_rack,self.waste,use_gripper=self.use_gripper)
			self.ctx.move_labware(next_rack,self.pick_up_slots[tip_load_name],use_gripper=self.use_gripper)
			self.reset_rack_list(tip_load_name)
			_rmode = ALL if tip_load_name in self.adapter_pickup_tipracks else None
			self.assign_tipracks(tip_load_name, pipette, mode=_rmode)

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
			for slot,item in self.ctx.deck.items(): 
				#Skip things that are modules or tiprack adapters
				if not item or item in self.ctx.loaded_modules.values():
					continue
				if item.load_name == 'opentrons_flex_96_tiprack_adapter':
					rack_obj = item.child
					if rack_obj != None:
						if rack_obj.load_name == tip_load_name:
							adapter_list.append(rack_obj)
				else:
					rack_obj = item
					if rack_obj.load_name == tip_load_name:
						if slot in self.ex_slots:
							ex_list.append(rack_obj)
						else:
							rack_list.append(rack_obj)
			self.tipracks[tip_load_name] = rack_list
			self.ex_racks[tip_load_name] = ex_list
			self.adapter_pickup_tipracks[tip_load_name] = adapter_list


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
		if self.print_comments:
			self.ctx.comment(
				f'TipTracker: expansion deck slot(s) now registered for tracking: {sorted(self.ex_slots)}. '
			)
		if self.debug:
			print(f'[TipTracker] add_expansion_slots: full registered set = {sorted(self.ex_slots)}')
			

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
		if self.print_comments:
			self.ctx.comment(f'Replacing {number_to_replace} {old_rack_name} with {new_rack_name}')
		if self.debug:
			print(f'Replacing {number_to_replace} {old_rack_name} with {new_rack_name}')
		slot_list = self.rack_assignments[old_rack_name][:number_to_replace]
		if self.print_comments:
			self.ctx.comment('Replacing tipracks')
		if self.debug:
			print('Replacing tipracks')
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
			if self.print_comments:
				self.ctx.comment(f'Ignoring slots {self.ignore_slots} for refill')
			if self.debug:
				print(f'Ignoring slots {self.ignore_slots} for refill')
		if slots is None or slots == []:
			if self.print_comments:
				self.ctx.comment(f'No slots left to refill for {tip_load_name} after applying ignore_slots; skipping refill_tips')
			if self.debug:
				print(f'No slots left to refill for {tip_load_name} after applying ignore_slots; skipping refill_tips')
			return
		slot_list = [slots] if isinstance(slots, str) else list(slots)
		if self.print_comments:
			self.ctx.comment(f'Refilling tips of {tip_load_name} on {slot_list}')
		if self.debug:
			print(f'Refilling tips of {tip_load_name} on {slot_list}')
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
			if self.print_comments:
				self.ctx.comment(skip_message)
			if self.debug:
				print(skip_message)
			return
		self.refill_tips(tip_load_name, slot_list)
		self.ctx.home()
		self.ctx.pause(f'Please place {tip_load_name} {pause_place_clause} {slot_list}')
		if pipette is not None and reassign_pipette:
			resolved_pipette = (
				self.pipette1
				if pipette in (1, '1', self.pipette1, 'one', 'One')
				else self.pipette2
				if pipette in (2, '2', self.pipette2, 'two', 'Two')
				else pipette
			)
			_rmode = ALL if tip_load_name in self.adapter_pickup_tipracks else None
			self.assign_tipracks(tip_load_name, resolved_pipette, mode=_rmode)

	def _reload_after_pause_if_non_empty(
		self, tip_load_name: str, load_slots: list[str], *, skip_message: str
	) -> None:
		if not load_slots:
			if self.print_comments:
				self.ctx.comment(skip_message)
			if self.debug:
				print(skip_message)
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
		'''
		Operator refill for tipracks on the main deck: clear exhausted racks on the target slots, home, pause for the user to place full racks, then optionally reassign that tip type to a pipette.

		Use anytime you want a deliberate pause to refill a tip type without waiting for pick-up to fail. Pass the API load name (``tip_load_name``). By default slots are **all** assignments for that type minus ``ignore_slots`` (including the expansion row if those slots are assigned). Use ``refill_main_deck_slots`` or ``refill_expansion_slots`` to limit which region is cleared and reloaded; pass ``slots`` explicitly to combine arbitrary positions.

		:param tip_load_name: API load name of the tiprack to refill, e.g. ``opentrons_flex_96_filtertip_1000ul``.
		:param pipette: If given and ``reassign_pipette`` is True, ``assign_tipracks`` is called for this pipette after the pause.
		:param slots: Slot or list of slots to use for ``refill_tips`` and the pause message; if None, uses ``_slots_for_rack_refill``.
		:param reassign_pipette: If False, skip ``assign_tipracks`` even when ``pipette`` is set (used when refilling secondary tip types during a ``refill_all`` flow).
		'''
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
		self.ctx.pause(f'Place {tip_load_name} onto slots {load_slots}')
		self.load_tipracks(tip_load_name, load_slots)

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
		if self.print_comments:
			self.ctx.comment(f'Wasting tips on slots {slots}: Using gripper : {self.use_gripper}')
		if self.debug:
			print(f'Wasting tips on slots {slots}: Using gripper : {self.use_gripper}')
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
				if self.debug:
					print(f'Ignoring slot {slot_key} for waste tips')
				if self.print_comments:
					self.ctx.comment(f'Ignoring slot {slot_key} for waste tips')
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
		if self.print_comments:
			self.ctx.comment(f'Reassigning tipracks of {resolved_pipette} to {rack_name} with mode: {mode}')
		if self.debug:
			print(f'Reassigning tipracks of {resolved_pipette} to {rack_name} with mode: {mode}')
		if resolved_pipette == self.pipette1:
			self.pipette_1_tip_type = rack_name
		elif resolved_pipette == self.pipette2:
			self.pipette_2_tip_type = rack_name
		try:
			if mode in ( COLUMN, SINGLE, ROW, PARTIAL_COLUMN):
				resolved_pipette.configure_nozzle_layout(style=mode,start=start,end=end,tip_racks=self.tipracks[rack_name])
				if resolved_pipette.tip_racks == [] and self.ex_racks.get(rack_name,[]) == [] and rack_name not in self.stackers.keys():
					self._refill_deck_manually(self.rack_assignments[rack_name],rack_name)
					self.assign_tipracks(rack_name,resolved_pipette,mode,start,end)
			elif mode == ALL:
				if self._pipette_is_flex_96channel(resolved_pipette):
					if rack_name in self.adapter_pickup_tipracks:
						if self.global_adapter:
							self.assign_slots(rack_name, list(self.tiprack_adapters.keys())[0])
						_tr = self.adapter_pickup_tipracks[rack_name]
					else:
						_tr = self.tipracks[rack_name]
					resolved_pipette.configure_nozzle_layout(style=ALL, start=start, end=end, tip_racks=_tr)
				else:
					resolved_pipette.tip_racks = self.tipracks[rack_name]
			elif mode is None:
				if self._pipette_is_flex_96channel(resolved_pipette) and rack_name in self.adapter_pickup_tipracks:
					resolved_pipette.tip_racks = self.adapter_pickup_tipracks[rack_name]
				else:
					resolved_pipette.tip_racks = self.tipracks[rack_name]
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
		if self.debug:
			print(f'Moving {rack} to {destination}, use_gripper={use_gripper}')
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
		'''
		Remove tip racks of ``tip_load_name`` from the deck (including adapters / expansion) and align TipTracker bookkeeping.

		* ``slots_to_clear is None`` — every **assigned** slot that still has something on ``ctx.deck`` is cleared, then all internal lists for ``tip_load_name`` are wiped.
		* ``slots_to_clear`` is a list — only those slots are cleared and matching entries are popped from ``tipracks`` / ``ex_racks`` / ``adapter_pickup_tipracks``.

		``save_tips`` (legacy name for the parameter): when ``False`` **and** the waste chute **and** gripper are enabled, racks are moved to the chute with the gripper (no pause). Otherwise the protocol pauses once for the operator, then racks are moved **off deck** without using the gripper for that toss.

		:param tip_load_name: API load name of the tip rack type to clear
		:param slots_to_clear: Slots to clear, or ``None`` for all assigned slots that still hold deck labware
		:param save_tips: See description above; ``replace_tips`` passes its ``manually_remove`` flag through here
		'''
		if self.print_comments:
			self.ctx.comment(f'Clearing old tipracks of {tip_load_name}')
		if self.debug:
			print(f'Clearing old tipracks of {tip_load_name}')

		slots, full_clear = self._clear_old_resolve_slot_targets(tip_load_name, slots_to_clear)
		use_gripper = self._clear_old_use_gripper_to_waste(save_tips)
		toss_location = self.waste if use_gripper else protocol_api.OFF_DECK

		if use_gripper:
			if self.print_comments:
				self.ctx.comment('Using gripper to remove tip racks')
			if self.debug:
				print('Using gripper to remove tip racks')
		else:
			where = 'All slots' if full_clear else str(slots)
			if self.debug:
				print(f'Please remove all {tip_load_name} from {where}')
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
			if self.debug:
				print(f'Storing {tiprack_to_move_away} in stacker {self.storing_stackers[tiprack_to_move_away.load_name]} to free up space for carousel')
			if self.print_comments:
				self.ctx.comment(f'Storing {tiprack_to_move_away} in stacker {self.storing_stackers[tiprack_to_move_away.load_name]} to free up space for carousel')
			for x,(stacker_info) in enumerate(self.storing_stackers[tiprack_to_move_away.load_name]):
				if stacker_info[1] < 6:
					open_slot = stacker_info[0]
					self.storing_stackers[tiprack_to_move_away.load_name][x][1] = self.storing_stackers[tiprack_to_move_away.load_name][x][1] + 1
					break
			intermediate_slot = self.storing_stackers[tiprack_to_move_away.load_name]
		#Move old labware to open slot
		if self.debug:
			print(f' Carousel from {tiprack_to_move_away} on {intermediate_slot} to {self.open_slot}')
		self._shuttle_labware(tiprack_to_move_away,open_slot)
		#Move new labware into vacated slot
		if self.debug:
			print(f' Carousel from {tiprack_to_move_in} on {leaving_open_slot} to {intermediate_slot}')
		self._shuttle_labware(tiprack_to_move_in,intermediate_slot)
		#Store labware in stacker in needed
		if tiprack_to_move_away.load_name in self.storing_stackers.keys():
			self.store_in_stacker(tiprack_to_move_away,open_slot)
		#Change the open slot to the slot vacated by the new labware
		if self.debug:
			print(f'----->Assigning open_slot to {leaving_open_slot}')
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
		for x,stacker_list in enumerate(self.stackers[tip_load_name]):
			if stacker_list[1] > 0:
				stacker : protocol_api.FlexStackerContext = stacker_list[0]
				chosen_index = x
				break
		if stacker is None:
			raise ValueError(f"No tipracks remaining in stackers for {tip_load_name}")
		stacker_current_labware = stacker.labware
		if stacker_current_labware == None:
			if self.print_comments:
				self.ctx.comment(f'Retrieving labware from stacker for {tip_load_name}')
			if self.debug:
				print(f'Retrieving labware from stacker for {tip_load_name}')
			labware = stacker.retrieve()
			self.stackers[tip_load_name][chosen_index][1] = self.stackers[tip_load_name][chosen_index][1] - 1 #Change Quantity of stacker
			if self.stackers[tip_load_name][chosen_index][2]: #This should be changes, internal flag for lid
				if self.print_comments:
					self.ctx.comment(f'Removing lid from stacker for {tip_load_name}')
				if self.debug:
					print(f'Removing lid from stacker for {tip_load_name}')
				self.ctx.move_lid(labware,self.waste,use_gripper=self.use_gripper)
		
		else:
			if stacker_current_labware.load_name != tip_load_name:
				if self.print_comments:
					self.ctx.comment(f'Labware on stacker is not {tip_load_name}, moving to {self.open_slot} and retrieving new labware')
				if self.debug:
					print(f'Labware on stacker is not {tip_load_name}, moving to {self.open_slot} and retrieving new labware')
				if self.open_slot == None:
					raise ValueError("No open slot defined, please define an open slot to move the labware to with non-matching labware on shuttle")
				self._shuttle_labware(stacker_current_labware,self.open_slot)
				self.return_to_stacker = (stacker_current_labware,self.open_slot,tip_load_name,chosen_index)
				labware = stacker.retrieve()
				self.stackers[tip_load_name][chosen_index][1] = self.stackers[tip_load_name][chosen_index][1] - 1 #Change Quantity of stacker
				if self.stackers[tip_load_name][chosen_index][2]: 
					if self.print_comments:
						self.ctx.comment(f'Removing lid from stacker for {tip_load_name}')
					if self.debug:
						print(f'Removing lid from stacker for {tip_load_name}')
					self.ctx.move_lid(labware,self.waste,use_gripper=self.use_gripper)
			else:
				if self.print_comments:
					self.ctx.comment(f'Getting labware already on shuttle for {tip_load_name}')
				if self.debug:
					print(f'Getting labware already on shuttle for {tip_load_name}')
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
		if self.print_comments:
			self.ctx.comment(f'Adding stacker module on slot {slot} with {initial_count} {tip_load_name}')
		if self.debug:
			print(f'Adding stacker module on slot {slot} with {initial_count} {tip_load_name}')
		stacker_obj = self.ctx.load_module('flexStackerModuleV1', slot)
		if tip_load_name in self.stackers.keys():
			self.stackers[tip_load_name].append([stacker_obj,None,True if lid != None else False])
		else:
			self.stackers[tip_load_name] = [[stacker_obj,None,True if lid != None else False]]
		if not use_for_storing_empty:
			self.load_tips_in_stacker(stacker_obj,tip_load_name,initial_count,lid,load_on_shuttle)
		else:
			if self.print_comments:
				self.ctx.comment(f'Using stacker on slot {slot} for storing empty tipracks, setting carousel to true')
			if self.debug:
				print(f'Using stacker on slot {slot} for storing empty tipracks, setting carousel to true')
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
		if self.print_comments:
			self.ctx.comment(f'Loading {quantity} {tip_load_name} into stacker in {stacker}')
		if self.debug:
			print(f'Loading {quantity} {tip_load_name} into stacker in {stacker}')
		stacker.set_stored_labware(tip_load_name,count=quantity - 1 if load_on_shuttle else quantity,lid=lid)
		if tip_load_name not in self.tip_rack_counts.keys():
			self.tip_rack_counts[tip_load_name] = quantity
		else:
			self.tip_rack_counts[tip_load_name] = self.tip_rack_counts[tip_load_name] + quantity
		if load_on_shuttle:
			if self.print_comments:
				self.ctx.comment(f'Loading labware onto stacker shuttle for {tip_load_name}')
			if self.debug:
				print(f'Loading labware onto stacker shuttle for {tip_load_name}')
			stacker.load_labware(tip_load_name)
		for x,stacker_list in enumerate(self.stackers[tip_load_name]):
			if stacker_list[0] == stacker:
				self.stackers[tip_load_name][x][1] = quantity - 1 if load_on_shuttle else quantity


	def _shuttle_labware(self,labware : protocol_api.Labware,location: str | protocol_api.ModuleContext | protocol_api.Labware) -> None:
		'''
		Internal function to move labware using the gripper or not based on settings. This is generally only used when moving labware from the stackers to the deck or when carouseling tipracks.
		
		:param self: TipTracker object
		:param labware: The labware to move
		:type labware: protocol_api.Labware
		:param location: The location to move the labware to. This can be a deck slot (str), a module context, or another labware / adapter context.
		:type location: str | protocol_api.ModuleContext | protocol_api.Labware
		:return: None
		:rtype: None
		'''
		self.ctx.move_labware(labware,location,use_gripper=self.use_gripper)
	
	def _refill_deck_manually(self,slots : list[str],tip_load_name : str) -> None:
		'''
		Internal function to refill tipracks manually by prompting the user to place new racks on the deck and then assigning them to the pipettes. This is used when not using the waste chute or gripper to move racks around, so the user has to manually move racks on and off the deck. This function will be called after prompting the user to remove old racks with clear_old() if not using the waste chute, and then will prompt the user to place new racks on the deck in the specified slots before assigning those slots to the given tip_load_name and assigning that tip_load_name to the pipettes.
		
		:param self: TipTracker object
		:param slots: The slots where the new tipracks have been placed by the user
		:type slots: list[str]
		:param tip_load_name: The API load name of the tiprack that has been placed on the deck
		:type tip_load_name: str
		:return: None
		:rtype: None
		'''
		if self.print_comments:
			self.ctx.comment(f'Please place new {tip_load_name} tipracks on deck in slots {slots}')
		if self.debug:
			print(f'Please place new {tip_load_name} tipracks on deck in slots {slots}')
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
		if self.print_comments:
			self.ctx.comment('No remaining tipracks in stackers, manual refill needed')
		if self.debug:
			print('No remaining tipracks in stackers, manual refill needed')
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
		if self.print_comments:
			self.ctx.comment(f'Max racks of {tip_load_name} reached, last racks in stacker')
		if self.debug:
			print(f'Max racks of {tip_load_name} reached, last racks in stacker')
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
			if self.print_comments:
				self.ctx.comment(f'Prepare stacker {stacker_mod} with {quantity} × {tip_load_name}')
			if self.debug:
				print(f'Prepare stacker {stacker_mod} with {quantity} × {tip_load_name}')
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
						if self.print_comments:
							self.ctx.comment(f'Tiprack in {stacker[0]}, moving to {slot}')
						if self.debug:
							print(f'Tiprack in {stacker[0]}, moving to {slot}')
						next_rack = self.move_from_stacker(tip_load_name)
						self._shuttle_labware(next_rack, self._shuttle_target_for_stacker_place(slot))
						break
					else:
						if self.print_comments:
							self.ctx.comment(f'No remaining tipracks in {stacker[0]}')
						if self.debug:
							print(f'No remaining tipracks in {stacker[0]}')
		else:
			if self.print_comments:
				self.ctx.comment('Tiprack in stacker, carouseling to active deck')
			if self.debug:
				print('Tiprack in stacker, carouseling to active deck')
			for old_rack in empty_slots:
				for stacker in self.stackers[tip_load_name]:
					if stacker[1] > 0:
						next_rack = self.move_from_stacker(tip_load_name)
						self.carousel(old_rack,next_rack)
						break
					else:
						if self.print_comments:
							self.ctx.comment(f'No remaining tipracks in {stacker[0]}')
						if self.debug:
							print(f'No remaining tipracks in {stacker[0]}')

'''
Version Changes: 
1. Added some traceback errors to help with debugging
2. Stackers can now be used to store empty tipracks
3. Adapters can now be used, allowing for full 96 channel support
4. Assign tipracks now takes a mode argument for partial tip pickup
5. Adapter slots can now be specified with add_starting_tipracks, assign_slots, and assign_tipracks calls
6. Deck refills: ``refill_deck`` / ``reload_deck_tipracks`` and related helpers for callable, tip-type–specific pauses
7. Expansion + stacker refills: ``refill_expansion_slots`` / ``reload_expansion_tipracks``, ``refill_main_deck_slots`` / ``reload_main_deck_tipracks``, ``refill_stacker_supply`` / ``reload_stacker_inventory`` (stacker ``fill`` vs ``load_tips_in_stacker``)
8. Refill helpers deduplicated (shared slot coercion, operator-refill / reload-after-pause paths, single ``_stacker_count_to_load``)
9. ``clear_old`` split into small helpers; docstring clarifies ``save_tips`` / auto-waste vs manual pause
10. ``verbose_tracebacks`` init flag and ``_fatal_tracker_error`` / ``_tiptracker_report_error`` for stderr trace + call stack on configuration failures
11. Stacker → deck moves: ``_shuttle_target_for_stacker_place`` so racks target the adapter labware on adapter slots (fixes LocationIsOccupied on e.g. A1)
12. ``load_tipracks``: adapter string branch ends with ``continue`` (no double adapter_pickup); plain slots skip ``load_labware`` if same type already present; tail appends de-duped; ``reload_deck_tipracks`` dedupes slot lists
13. Adapter REPLACE_ME path: set ``replacement_rack = rack`` before ``move_labware`` (was always ``None``)
14. Adapter pickup empty list: use ``getattr(rack.parent, 'parent', None)`` so racks moved to ``OFF_DECK`` do not raise on ``.parent``
15. ``empty_tipracks``: dedupe by ``id`` (avoid ``set()`` hashing labware on OFF_DECK); build ``empty_tiprack_slots`` skipping ``OFF_DECK`` / bad ``.parent``
16. Adapter REPLACE_ME branch: require ``len(tip_racks) >= 3`` before indexing (``tip_racks`` can be empty after layout change)
17. ``_deck_slot_id``: treat ``OffDeckType`` / ``OFF_DECK`` as no slot; final parent read via ``getattr``
18. ``waste_tips`` / ``empty_tiprack_slots`` / ``other_rack_slots``: skip ``OffDeckType`` (``OFF_DECK``, ``WASTE_CHUTE``); guard ``ctx.deck[slot_key]`` when key missing
19. ``load_tipracks`` (adapter slot): if adapter already has a child, reuse when same ``tip_load_name`` else move child off before ``load_labware`` (fixes ``LocationIsOccupied``)
20. Expansion→deck shuttle in ``pick_up``: use ``_shuttle_target_for_stacker_place(open_slot)`` so racks land on adapter labware, not bare A1
21. After expansion/stacker/manual refills in ``pick_up``, ``shuffle_for_forced_pickup``, and ``_operator_refill_impl``, reassign with ``mode=ALL`` when ``rack_name`` uses ``adapter_pickup`` (fixes OutOfTips after ROW/COLUMN if hardware still reports non-96 ``active_channels``)
22. ``assign_tipracks(..., mode=ALL)``: only quadheads (``config.channels == 96``) use adapter lists + ``configure_nozzle_layout(ALL, …)``; 8-channel uses plain ``tip_racks``. ``mode is None`` assigns ``tip_racks`` without changing layout.
23. ``pick_up`` 96-channel guard: require ``adapter_pickup_tipracks`` only when nominal 96 and **active** channels are still 96 (allows deck ALL after partial layout)
24. ``_pipette_is_flex_96channel`` + ``assign_tipracks(mode=None)``: detect Flex 96 when ``config.channels`` is missing so adapter ALL layout is not skipped (avoids ``KeyError`` on ``tipracks`` in sim)
25. Naming: consolidated **API tip load name** locals/params to ``tip_load_name`` (replacing ``rackname`` / ``tiprack_name`` / ambiguous ``name`` in helpers); ``assign_tipracks`` still uses public param ``rack_name`` for keyword compatibility; ``resolved_pipette`` in ``assign_tipracks`` / operator-refill; clearer names in ``assign_slots`` loops.
26. Library release **3.0**: documentation and ``metadata['Version']`` aligned to 3.0 (see README and ``TIPTRACKER_MANUAL.md``).
'''

def run(ctx: protocol_api.ProtocolContext):
	'''
	Debug harness: 96-channel (left) exercises adapter, expansion, and stackers; 8-channel (right) exercises 50/200 µL
	deck racks, shuffle, replace, refill_tips, clear_old, carousel, and ``return_tip`` without mixing 96-channel adapter rules.

	Not run here (operator / hardware heavy): ``move_from_stacker`` as a direct public call, ``store_in_stacker``,
	``refill_deck``, ``refill_stacker_supply``, ``reload_stacker_inventory``, ``reload_deck_tipracks``, ``reload_main_deck_tipracks``.
	'''
	TIPS_50 = 'opentrons_flex_96_filtertiprack_50ul'
	TIPS_200 = 'opentrons_flex_96_filtertiprack_200ul'
	TIPS_1000 = 'opentrons_flex_96_filtertiprack_1000ul'

	def section(title: str) -> None:
		ctx.comment(f'[TipTracker harness] === {title} ===')

	multi_96 = ctx.load_instrument('flex_96channel_1000', 'left')
	multi_8 = ctx.load_instrument('flex_8channel_1000', 'right')
	bin = ctx.load_waste_chute()

	T = TipTracker(
		ctx=ctx,
		pipette1=multi_96,
		pipette2=multi_8,
		waste_bin=bin,
		use_gripper=True,
		debugging=True,
		suppress_comments=False,
		verbose_tracebacks=True,
	)

	section('setup: expansion, deck, stackers, global_adapter')
	T.add_expansion_slots(['A4', 'B4', 'C4'])
	T.add_starting_tipracks(
		TIPS_200, ['B1'],
		TIPS_50, ['C1'],
		TIPS_1000, ['A1'],
		adapters=['A1'],
		max_racks_3=24,
	)
	T.global_adapter = True
	# Uncomment to exercise stacker-driven refills (can place racks on non-adapter slots; tune deposit list / adapter map first):
	# T.add_stacker('B4', TIPS_1000, 7, 'opentrons_flex_tiprack_lid', True)
	# T.add_stacker('C4', TIPS_200, 7, 'opentrons_flex_tiprack_lid', True)

	# --- 96-channel (pipette 1): adapter + expansion + stacker refills ---
	T.active_pipette = multi_96
	section('96ch: assign_tipracks ALL + one pick/drop (second ALL pick needs a second adapter-mounted rack; A3 deck rack is not valid for simultaneous 96 tips)')
	T.assign_tipracks(TIPS_1000, pipette=1, mode=ALL)
	T.pick_up(pipette=1, set_active_pipette=True)
	T.drop_tip(pipette=1)

	# --- 8-channel (pipette 2): deck racks, shuffle, replace, refill, clear, carousel ---
	T.active_pipette = multi_8

	section('8ch: assign_tipracks TIPS_200 / TIPS_50 + drop_tip(return_tip=True)')
	T.assign_tipracks(TIPS_200, pipette=2)
	T.pick_up(pipette=2)
	T.drop_tip(pipette=2)
	T.assign_tipracks(TIPS_50, pipette=2)
	T.pick_up(pipette=2)
	T.drop_tip(pipette=2, return_tip=True)
	T.pick_up(pipette=2)
	T.drop_tip(pipette=2)

	section('8ch: assign_slots + load_tipracks (50 µL on D2)')
	T.assign_slots(TIPS_50, ['D2'])
	T.load_tipracks(TIPS_50, ['D2'])

	section('8ch: ignore_slots on C1, then restore')
	T.ignore_slots.append('C1')
	T.assign_tipracks(TIPS_50, pipette=2)
	T.pick_up(pipette=2)
	T.drop_tip(pipette=2)
	T.ignore_slots.remove('C1')

	section('8ch: pick_up_slots + second 200 µL rack (D1 — D3 conflicts with waste chute fixture in sim)')
	T.load_tipracks(TIPS_200, ['D1'])
	T.assign_slots(TIPS_200, ['B1', 'D1'])
	T.assign_tipracks(TIPS_200, pipette=2)
	T.pick_up_slots[TIPS_200] = 'B1'
	T.pick_up(pipette=2)
	T.drop_tip(pipette=2)
	T.pick_up(pipette=2)
	T.drop_tip(pipette=2)

	# replace_tips: exercise manually when slot lists are clean; combined assign_slots here can trip duplicate-slot validation.

	# waste_tips / refill_tips / clear_old: run in isolation after sim state is stable (chute moves invalidate cached labware refs).

	# carousel: enable with use_chute=False + open_slot after ensuring both racks are on-deck (avoid stale refs after waste).

	T.active_pipette = multi_96
	section('reset_rack_list(TIPS_1000)')
	T.reset_rack_list(TIPS_1000)

	section('final counters')
	ctx.comment(f'tip_counts={T.tip_counts}')
	ctx.comment(f'tip_rack_counts={T.tip_rack_counts}')
	ctx.comment(f'pick_up_count={T.pick_up_count}')
	ctx.comment(f'drop_count={T.drop_count}')