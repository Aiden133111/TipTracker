from opentrons import protocol_api
from opentrons.protocol_api.labware import OutOfTipsError
from opentrons.protocol_api import ALL, COLUMN, SINGLE, ROW, PARTIAL_COLUMN
from opentrons.types import NozzleConfigurationType
from traceback import print_exc

#PROTOCOL REQUIREMENTS
metadata = {
	'protocolName': 'Tip Tracking Class',
	'author': 'Aiden McFadden, Opentrons',
	'source': 'Custom Protocol Development',
	'description' : 'Goal is to create a method to track tips across a run in a flexible fashion',
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

	def __init__(self, ctx : protocol_api.ProtocolContext, pipette1 : protocol_api.InstrumentContext, 
				waste_bin : protocol_api.WasteChute | protocol_api.TrashBin, pipette2 : protocol_api.InstrumentContext | None = None,
				use_gripper : bool = False, debugging : bool = False, suppress_comments : bool = False) -> None:
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
		'''

		self.metadata = {
			'Author': 'Aiden McFadden',
			'Version' : '2.2',
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
		#A list of deck slot names that should be ignored by the tracker for throwing away and refill, just keep on deck useful for tip reuse. Read/Write okay ex TipTracker.ignore_slots.append('A3').
		self.ignore_slots : list[str] = []
		#A dictionary of tiprack load names and the slots they should only pick up from to force a pickup in a given slot, useful for partial tip pickups.
		#Read/Write okay TipTracker.pick_up_slots['opentrons_flex_96_tiprack_50ul'] = 'A1'
		self.pick_up_slots : dict[str, str] = {}
		#A default pipette that should be used to prevent having to pass pipette repeatedly to pickup and drop commands. Read/Write okay TipTracker.active_pipette = self.pipette1 can also be
		#set during pick_up commands using the set_active_pipette argument.
		self.active_pipette = None		
		#If a single adapter should be used for all tipracks. If true, the first adapter loaded will be used for all tipracks and it will be auto assigned to the most recent pickup call
		self.global_adapter : bool = False		
															

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
		all_tipracks = [tiprack1, tiprack2, tiprack3, tiprack4]
		for x, slot in enumerate(all_slots):
			if type(slot) != list:
				all_slots[x] = [slot]
		try:
			if len(set([tiprack for tiprack in all_tipracks if tiprack != None])) != len([tiprack for tiprack in all_tipracks if tiprack != None]):
				raise ValueError("Duplicate tiprack types detected, please ensure all tiprack slots are added under one tiprack argument")
			if len(set([slot for slot_list in all_slots for slot in slot_list if slot != None])) != len([slot for slot_list in all_slots for slot in slot_list if slot != None]):
				raise ValueError("Duplicate slots detected, please ensure all slots are unique across tiprack arguments")
		except ValueError as Error:
			print_exc()
			print('Error with slot assignments:', Error)
			exit(1)
		for tiprack,slots in zip(all_tipracks, all_slots):
			try:
				if tiprack == None and slots == [None]:
					continue
				if tiprack == None and slots != [None]:
					raise ValueError("Slots provided without a corresponding tiprack")
				if tiprack != None and slots == [None]:
					raise ValueError("Tiprack provided without a corresponding slot or slots")
			except ValueError as Error:
				print_exc()
				print('Slot assignment error:', Error)
				exit(1)
			for other_rack, other_slot_list in self.rack_assignments.items():
				if other_rack != tiprack:
					if any(slot in other_slot_list for slot in slots):
						print(f'Slot conflict detected for {tiprack} in slots {slots} with {other_rack} in slots {other_slot_list}')
						self.rack_assignments[other_rack] = [slot for slot in other_slot_list if slot not in slots]
			for slot in slots:
				if slot in self.tiprack_adapters.keys():
					if self.print_comments:
						self.ctx.comment(f'Overwriting adapter on slot {slot} from {self.tiprack_adapters[slot][0]} to {tiprack}')
					if self.debug:
						print(f'Overwriting adapter on slot {slot} from {self.tiprack_adapters[slot][0]} to {tiprack}')
					self.tiprack_adapters[slot][0] = tiprack
					if tiprack not in self.adapter_pickup_tipracks.keys():
						self.adapter_pickup_tipracks[tiprack] = [f'REPLACE_ME',tiprack,slot]
			if clear_other_slots or tiprack not in self.rack_assignments.keys():
				self.rack_assignments[tiprack] = slots
			else:
				self.rack_assignments[tiprack].extend(slots)
		


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

		#Load labware for each tiprack in each slot
		for rackname,slots in zip([tiprack1, tiprack2, tiprack3, tiprack4],[slots1, slots2, slots3, slots4]):
			if rackname != None:
				for slot in slots:
					if self.max_racks_count.get(rackname,None) != None:
						if self.max_racks_count[rackname] == self.tip_rack_counts.get(rackname,0):
							if self.print_comments:
								self.ctx.comment(f'Max racks of {rackname} reached, not loading more')
							if self.debug:
								print(f'Max racks of {rackname} reached, not loading more')
							continue
					if type(slot) == str:
						if slot in adapters or slot in self.tiprack_adapters.keys():
							if self.tiprack_adapters.get(slot,None) == None:
								if self.debug:
									print(f'Loading adapter for {rackname} in slot {slot}')
								if self.print_comments:
									self.ctx.comment(f'Loading adapter for {rackname} in slot {slot}')
								adapter = self.ctx.load_adapter('opentrons_flex_96_tiprack_adapter',slot)
								rack = adapter.load_labware(rackname)
								self.tiprack_adapters[slot] = [rackname, adapter]
							else:
								if self.debug:
									print(f'Adapter already on slot {slot}, loading {rackname} onto adapter')
								if self.print_comments:
									self.ctx.comment(f'Adapter already on slot {slot}, loading {rackname} onto adapter')
								adapter = self.tiprack_adapters[slot][1]
								rack = adapter.load_labware(rackname)
								self.tiprack_adapters[slot] = [rackname, adapter]
							if rackname in self.adapter_pickup_tipracks.keys():
								self.adapter_pickup_tipracks[rackname].append(rack)
							else:
								self.adapter_pickup_tipracks[rackname] = [rack]

						else:
							rack = self.ctx.load_labware(rackname, slot)
						if rackname not in self.tip_rack_counts.keys():
							self.tip_rack_counts[rackname] = 1
						else:
							self.tip_rack_counts[rackname] = self.tip_rack_counts[rackname] + 1
					elif type(slot) == protocol_api.Labware and slot.load_name == 'opentrons_flex_96_tiprack_adapter':
						rack = slot.load_labware(rackname)
						self.tiprack_adapters[slot.parent][0] = rackname
						if self.debug:
							print(f'Loading {rackname} onto adapter in slot {slot.parent}')
						if self.print_comments:
							self.ctx.comment(f'Loading {rackname} onto adapter in slot {slot.parent}')
						if rackname in self.adapter_pickup_tipracks.keys():
							self.adapter_pickup_tipracks[rackname].append(rack)
						else:
							self.adapter_pickup_tipracks[rackname] = [rack]
					if self.ex_slots != None and slot in self.ex_slots:
						if rackname in self.ex_racks.keys():
							self.ex_racks[rackname].append(rack)
						else:
							self.ex_racks[rackname] = [rack]
					elif slot in self.tiprack_adapters.keys():
						if rackname in self.adapter_pickup_tipracks.keys():
							self.adapter_pickup_tipracks[rackname].append(rack)
						else:
							self.adapter_pickup_tipracks[rackname] = [rack]
					else:
						if rackname in self.tipracks.keys():
							self.tipracks[rackname].append(rack)
						else:
							self.tipracks[rackname] = [rack]


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
			if active_pipette == None:
				raise ValueError(f"Invalid pipette: {pipette}, must be in [1,'1',self.pipette1,'one','One'] or [2,'2',self.pipette2,'two','Two']")
			if pipette == None and self.active_pipette == None:
				raise ValueError(f"Active pipette not set but no pipette argument was passed, please set active pipette or specify pipette in call")
			rack_name = self.pipette_1_tip_type if active_pipette == self.pipette1 else self.pipette_2_tip_type
			if rack_name is None:
				raise ValueError(f"No tipracks assigned to pipette {active_pipette}, please assign tipracks before picking up tips")
		except ValueError as Error:
			print_exc()
			print('Error with pipette or tiprack assignment:', Error)
			exit(1)
		#######################################################################
		#Tip Data structures in case we need to refill or move tipracks around#
		#######################################################################
		#Slots associated with the rack_name that we use to check for refills 
		slots_to_check = [slot for slot in self.rack_assignments[rack_name] if slot not in self.ignore_slots]
		#Slots assigned to the tiprack that do not have a tiprack physically on them (for replacement)
		vacant_slots = [slot for slot in self.rack_assignments[rack_name] if self.ctx.deck[slot] == None and slot not in self.ignore_slots and slot not in self.tiprack_adapters.keys()] # Get the slots that are empty and can be loaded with racks
		#Tiprack objects that have a rack on them but with no tips (throw in trash or shuffle)
		empty_tipracks = [self.ctx.deck[slot] for slot in slots_to_check if self.ctx.deck[slot] != None and self.ctx.deck[slot].load_name != 'opentrons_flex_96_tiprack_adapter' and not any([well.has_tip for well in self.ctx.deck[slot].wells()])] # Get the racks on the deck that have no tips and are not in ignored slots
		if self.adapter_pickup_tipracks.get(rack_name,[]) != [] and self.adapter_pickup_tipracks.get(rack_name,[])[0] != 'REPLACE_ME': 
			empty_tipracks = empty_tipracks + [rack.parent for rack in self.adapter_pickup_tipracks[rack_name] if not any([well.has_tip for well in rack.wells()]) and rack.parent.parent not in self.ignore_slots] # Get the racks on the adapters that have no tips and are not in ignored slots
		#Slots assigned to the tiprack that have a tiprack but no tips (for replacement after tossing / shuffling)
		if rack_name in self.adapter_pickup_tipracks.keys():
			for slot, datalist in self.tiprack_adapters.items():
				if datalist[-1].child == None and datalist[-1] not in vacant_slots:
					vacant_slots.append(slot)
				if datalist[-1].child != None:
					if not any([well.has_tip for well in datalist[-1].child.wells()]) and datalist[-1] not in empty_tipracks:
						empty_tipracks.append(datalist[-1].child)
		empty_tipracks = list(set(empty_tipracks))
		vacant_slots = list(set(vacant_slots))
		empty_tiprack_slots = [rack.parent for rack in empty_tipracks] # Get the slots of the racks on the deck that have no tips and are not in ignored slots
		if refill_all:
			other_rack_slots = { rack_load_name : [rack.parent for rack in rack_list if not any([well.has_tip for well in rack.wells()])] for rack_load_name,rack_list in self.tipracks.items() if rack_load_name != rack_name} # Move these to waste
			empty_tip_slots = {rack_load_name : [slot for slot in racklist if self.ctx.deck[slot] == None] for rack_load_name, racklist in self.rack_assignments.items()} # Load these plus other racks slots
		#######################################################################################################################################################################
		#Updates a deck in the edge case that an adapter pickup is empty and now has been reassigned to a different rack_name, this shuffles the correct rack onto the adapter#
		#######################################################################################################################################################################
		if active_pipette.tip_racks[0] == 'REPLACE_ME':
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
		if rack_name in self.pick_up_slots.keys():
			#Check if tiprack has tips first
			next_tip = self.ctx.deck[self.pick_up_slots[rack_name]].next_tip()
			if next_tip == None:
				if self.debug:
					print(f'No tips available for pickup on slot {self.pick_up_slots[rack_name]}, shuffling tipracks')
				if self.print_comments:
					self.ctx.comment(f'No tips available for pickup on slot {self.pick_up_slots[rack_name]}, shuffling tipracks')
				if rack_name in self.pick_up_slots:
					self.shuffle_for_forced_pickup(rack_name,self.pick_up_slots[rack_name], active_pipette)
		try:
			if active_pipette.active_channels == 96 and rack_name not in [data[0] for data in self.tiprack_adapters.values()]:
				raise ValueError(f'No adapter pickup tiprack defined for {rack_name} but pipette has 96 channels, please define an adapter pickup tiprack or change the tiprack type used for pickup')
		except ValueError as Error:
			print_exc()
			print(f'Error in tiprack or slot assignment: {Error}')
			exit(1)
		
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
			#Trash old tips
			if not self.carousel_tips: #Trash tips in waste chute if able
				self.waste_tips(empty_tipracks)
			if self.ex_racks.get(rack_name, None) == None and self.stackers.get(rack_name,None) == None:
				if self.print_comments:
					self.ctx.comment('No expansion slots / stackers defined, Refilling Manually') # Dont have to worry about carousel here, no ex slots
				if self.debug:
					print('No expansion slots / stackers defined, Refilling Manually')
				self.refill_tips(rack_name,slots_to_check)
				self.ctx.home()
				self.ctx.pause(f"Please place {rack_name} onto slots {slots_to_check}")
				self.assign_tipracks(pipette=active_pipette,rack_name=rack_name)
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
							self.ctx.home()
							self.ctx.pause(f"Please place {other_rack_names} onto slots {other_slots + empty_tip_slots[other_rack_names]}")
							self.refill_tips(other_rack_names,other_slots + empty_tip_slots[other_rack_names])
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
							self.ctx.home()
							self.ctx.pause(f"Please place {other_rack_names} onto slots {other_slots + empty_tip_slots[other_rack_names]}")
							self.refill_tips(other_rack_names,other_slots + empty_tip_slots[other_rack_names])
				if rack_name in self.ex_racks.keys() and self.ex_racks[rack_name] != []:
					if self.print_comments:
						self.ctx.comment('Tiprack on expansion slot, moving to active deck')
					if self.debug:
						print('Tiprack on expansion slot, moving to active deck')
					if self.carousel_tips:
						for old_rack,e_rack in zip(self.tipracks[rack_name],self.ex_racks[rack_name]):
							self.carousel(old_rack,e_rack)
							return_code = 1
					else:
						for e_rack, open_slot in zip(self.ex_racks[rack_name],empty_tiprack_slots): #This needs a check for if expansion slot has tips 
							e_slot_source = e_rack.parent
							self._shuttle_labware(e_rack, open_slot)
							if rack_name in self.empty_ex_slots.keys():
								self.empty_ex_slots[rack_name].append(e_slot_source)
							else:
								self.empty_ex_slots[rack_name] = [e_slot_source]
							return_code = 2
					self.reset_rack_list(rack_name)			
					self.assign_tipracks(pipette=active_pipette,rack_name=rack_name)
					
					active_pipette.pick_up_tip(locus)
				elif rack_name in self.stackers.keys() and sum([stacker[1] for stacker in self.stackers.get(rack_name, [])]) > 0:
					self.grab_from_stacker(rack_name, empty_tipracks)
					self.reset_rack_list(rack_name)
					self.assign_tipracks(pipette=active_pipette,rack_name=rack_name)
					active_pipette.pick_up_tip(locus)
					return_code = 3
				else:
					self.call_refill = True
					if rack_name in self.stackers.keys():
						self._refill_stacker_manually(rack_name, empty_tipracks)
					#This block is just for user information
					if len(self.ex_racks.get(rack_name, [])) > 0 and self.call_refill: 
						if self.print_comments:
							self.ctx.comment('No remaining tipracks on expansion deck, manual refill needed')
						if self.debug:
							print('No remaining tipracks on expansion deck, manual refill needed')
					#Home the robot and initiate a pause for a manual refill if we need to refill the deck as well
					self.ctx.home()
					if self.call_refill:
						if empty_tipracks != []:
							slot_list = [slot if type(slot) != protocol_api.Labware else slot.parent for slot in empty_tipracks]
							self.ctx.pause(f'Place {rack_name} onto slots {slot_list}')
						self.load_tipracks(rack_name, empty_tipracks)
					#Reset internal data and resign tips after a manual refill if needed
					self.reset_rack_list(rack_name)
					self.assign_tipracks(pipette=active_pipette,rack_name=rack_name)
					self.open_slot = self.original_open_slot
					active_pipette.pick_up_tip(locus)
					return_code =  4

		#Return labware to the shuttle it it had to be moved to the open slot during a tip refill
		if self.return_to_stacker:
			if self.print_comments:
				self.ctx.comment('Returning labware to stacker')
			if self.debug:
				print('Returning labware to stacker')
			stacker_original_labware, holding_slot, rackname, chosen_index  = self.return_to_stacker
			self._shuttle_labware(stacker_original_labware,self.stackers[rackname][chosen_index][0])
			self.return_to_stacker = False
		#Count the pick up for the tiptype and the pipette, return code for how a tip was picked up
		if rack_name in self.tip_counts.keys():
			self.tip_counts[rack_name] = self.tip_counts[rack_name] + active_pipette.active_channels
		else:
			self.tip_counts[rack_name] = active_pipette.active_channels
		self.pick_up_count[active_pipette] = self.pick_up_count[active_pipette] + 1
		return return_code
	

	def shuffle_for_forced_pickup(self, rack_name : str, pick_up_slot : str, pipette : protocol_api.InstrumentContext) -> None:
		'''
		This function will shuffle labware around the deck to force the next tip pickup for a rack type to be in its pick_up_slot. This function should generally only be used by the tracker itself \
		when a rack is out of tips and a manual refill is not needed. If there is a waste chute, the old labware will be thrown away. This function will update internal data after moving labware around the deck. \
		Forced pickup is most useful for partial tip pickups so that only the specified slots need spatial clearance for partial tip pickup.
		
		:param self: TipTracker object
		:param rack_name: The API load name of the tiprack type that should be shuffled into the forced pickup slot
		:type rack_name: str
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
		for slot in self.rack_assignments[rack_name]:
			if slot == pick_up_slot or self.ctx.deck[slot] == None:
				continue
			elif self.ctx.deck[slot]:
				next_rack = self.ctx.deck[slot]
				break
		if next_rack is None:
			raise ValueError(f"No other tiprack with tips found to shuffle into {pick_up_slot} for {rack_name}")
		if self.carousel_tips:
			self.carousel(empty_rack,next_rack)
			self.reset_rack_list(rack_name)
			self.assign_tipracks(pipette=pipette,rack_name=rack_name)
		elif self.use_chute:
			if self.print_comments:
				self.ctx.comment(f'Disposing of empty tiprack in {pick_up_slot} replacing with {next_rack.parent}')
			if self.debug:
				print(f'Disposing of empty tiprack in {pick_up_slot} replacing with {next_rack.parent}')
			self.ctx.move_labware(empty_rack,self.waste,use_gripper=self.use_gripper)
			self.ctx.move_labware(next_rack,self.pick_up_slots[rack_name],use_gripper=self.use_gripper)
			self.reset_rack_list(rack_name)
			self.assign_tipracks(pipette=pipette,rack_name=rack_name)

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
			print_exc()
			print('Error in tiprack or slot assignment:', Error)
			exit(1)
		for x, slot in enumerate(assign_slots):
			if type(slot) != list:
				assign_slots[x] = [slot]
		try:
			if len(set([tiprack for tiprack in tipracks if tiprack != None])) != len([tiprack for tiprack in tipracks if tiprack != None]):
				raise ValueError("Duplicate tiprack types detected, please ensure all tiprack slots are added under one tiprack argument")
			if len(set([slot for slot_list in assign_slots for slot in slot_list if slot != None])) != len([slot for slot_list in assign_slots for slot in slot_list if slot != None]):
				raise ValueError("Duplicate slots detected, please ensure all slots are unique across tiprack arguments")
		except ValueError as Error:
			print_exc()
			print('Error in tiprack or slot assignment:', Error)
			exit(1)

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
		for rack_name in rack_names:
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
						if rack_obj.load_name == rack_name:
							adapter_list.append(rack_obj)
				else:
					rack_obj = item
					if rack_obj.load_name == rack_name:
						if slot in self.ex_slots:
							ex_list.append(rack_obj)
						else:
							rack_list.append(rack_obj)
			self.tipracks[rack_name] = rack_list
			self.ex_racks[rack_name] = ex_list
			self.adapter_pickup_tipracks[rack_name] = adapter_list


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
		invalid_slots = [x for x in self.ex_slots if x not in ['A4','B4','C4','D4']]
		if len(invalid_slots) > 0:
			raise ValueError(f"Invalid expansion slots: {invalid_slots}, slots must be A4, B4, C4, or D4")
			

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


	def refill_tips(self, name : str , slots : list[str] | str, waste_all_old : bool = True) -> None:
		'''
		This function refills tipracks of a given name on the given slots. It will first clear the old racks from the deck, \
		either by moving them to the waste chute or off deck, then it will load new racks onto the deck and update the internal data. \
		Any slots in the ignore_slots list will be ignored for refilling, so if you have a rack that you want to stay on the deck (tip reuse), \
		add its slot to the ignore_slots list and it will be skipped over when refilling. \
		
		:param self: TipTracker object
		:param name: API Load name for the tiprack that you want to refill, for example opentrons_flex_96_filtertip_50ul
		:type name: str
		:param slots: List or string of slots where the tipracks are located that you want to refill
		:type slots: list[str] | str
		:param waste_all_old: Whether to waste all old tipracks before refilling, when True all slots assigned to the tiprack will be wasted if empty, when False only the slots being refilled will be wasted if empty. This only matters if using the waste chute, if not all racks will be moved off deck regardless
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
		if self.print_comments:
			self.ctx.comment(f'Refilling tips of {name} on {slots}')
		if self.debug:
			print(f'Refilling tips of {name} on {slots}')
		clear_slots = [slot for slot in slots if slot not in self.ignore_slots and self.ctx.deck[slot] != None ]
		clear_slots = [slot for slot in clear_slots if self.ctx.deck[slot].load_name != 'opentrons_flex_96_tiprack_adapter']
		self.clear_old(name,clear_slots if not waste_all_old else None,False)
		self.load_tipracks(name,slots)


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
		for slot in slots:
			if slot in self.ignore_slots:
				if self.debug:
					print(f'Ignoring slot {slot} for waste tips')
				if self.print_comments:
					self.ctx.comment(f'Ignoring slot {slot} for waste tips')
				continue
			if slot in self.tiprack_adapters.keys():
				labware_to_move = self.tiprack_adapters[slot][1].child
			elif type(slot) == protocol_api.Labware:
				if slot.load_name != 'opentrons_flex_96_tiprack_adapter':
					labware_to_move = slot
				else:
					labware_to_move = slot.child
			else:
				labware_to_move = self.ctx.deck[slot]
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
			pip = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
			if pip == None:
				raise ValueError(f"Invalid pipette number {pipette}, must be 1 or 2, as strings or integers or pipette objects")
		else:
			pip = self.active_pipette
		if self.print_comments:
			self.ctx.comment(f'Reassigning tipracks of {pip} to {rack_name} with mode: {mode}')
		if self.debug:
			print(f'Reassigning tipracks of {pip} to {rack_name} with mode: {mode}')
		if pip == self.pipette1:
			self.pipette_1_tip_type = rack_name
		elif pip == self.pipette2:
			self.pipette_2_tip_type = rack_name
		try:
			if mode in ( COLUMN, SINGLE, ROW, PARTIAL_COLUMN):
				pip.configure_nozzle_layout(style=mode,start=start,end=end,tip_racks=self.tipracks[rack_name])
				if pip.tip_racks == [] and self.ex_racks.get(rack_name,[]) == [] and rack_name not in self.stackers.keys():
					self._refill_deck_manually(self.rack_assignments[rack_name],rack_name)
					self.assign_tipracks(rack_name,pip,mode,start,end)
			elif mode == ALL:
				if self.global_adapter:
					self.assign_slots(rack_name, list(self.tiprack_adapters.keys())[0])
				pip.configure_nozzle_layout(style=mode,start=start,end=end,tip_racks=self.adapter_pickup_tipracks[rack_name])
			elif mode == None:
				if pip.active_channels < 96:
					pip.tip_racks = self.tipracks[rack_name]
				else:
					pip.tip_racks = self.adapter_pickup_tipracks[rack_name]
		except KeyError as Error:	
			print_exc()
			print(f'Error in assigning tipracks with given mode {mode}: {Error}, are you sure you defined tipracks for this mode?')
			exit(1)
		

	def clear_old(self,name : str ,slots_to_clear : None | list = None,save_tips = True) -> None:
		'''
		Clear old tipracks of a certain type from internal data and optionally from the deck. If slots_to_clear is None, will clear all \
		tipracks of that type from the deck and internal data. If slots_to_clear is a list of slots, will only clear those slots from the \
		deck and internal data. If save_tips is False, will use the waste chute to remove the tips instead of prompting the user to remove them.\

		:param self: TipTracker object
		:param name: The labware load name of the tiprack to clear
		:type name: str
		:param slots_to_clear: Optional list of slots to clear, if None will clear all tipracks of that type
		:type slots_to_clear: None | list
		:param save_tips: Bool, if False will use the waste chute to remove tips instead of prompting the user to remove them all in one message (ie no move_labware command)
		:type save_tips: bool
		:return: None
		:rtype: None
		'''		
		if self.print_comments:
			self.ctx.comment(f'Clearing old tipracks of {name}')
		if self.debug:
			print(f'Clearing old tipracks of {name}')
		if save_tips == False and self.use_chute == True and self.use_gripper == True:
			if self.print_comments:
				self.ctx.comment('Using Gripper to remove tips')
			if self.debug:
				print('Using Gripper to remove tips')
			toss_tips = True
			toss_location = self.waste
		else:
			slots_message = 'All slots' if slots_to_clear == None else str(slots_to_clear)
			if self.debug:
				print(f'Please remove all {name} from {slots_message}')
			self.ctx.pause(f'Please remove all {name} from {slots_message}')
			toss_tips = False
			toss_location = protocol_api.OFF_DECK
		if slots_to_clear == None:
			slots_to_clear = [slot for slot in self.rack_assignments.get(name,[]) if self.ctx.deck[slot] != None and slot not in self.ignore_slots]
			for slot in slots_to_clear:
				if slot in self.tiprack_adapters.keys():
					rack = self.tiprack_adapters[slot][1].child
				else:
					rack = self.ctx.deck[slot]
				if rack in self.ctx.loaded_modules.values():
					continue #prevent things on the stacker from being moved incorrectly when nothing is not the third column
				if self.debug:
					print(f'Moving {rack} from {slot} to waste: {toss_location}, using gripper: {toss_tips}')
				self.ctx._core.move_labware(
						labware_core=rack._core,
						new_location=toss_location,
						use_gripper=toss_tips,
						pause_for_manual_move=False,
						pick_up_offset=(0.0,0.0,0.0),
						drop_offset=(0.0,0.0,0.0))
			self.tipracks[name] = []
			if name in self.ex_racks.keys():
				self.ex_racks[name] = []
			if name in self.adapter_pickup_tipracks.keys():
				self.adapter_pickup_tipracks[name] = []
		else:
			pop_these_active_deck = []
			pop_these_expansion_slots = []
			pop_these_adapter_slots = []
			if name in self.tipracks.keys() or name in self.ex_racks.keys() or name in self.adapter_pickup_tipracks.keys():
				labware_to_move = []
				for slot in slots_to_clear:
					if slot in self.tiprack_adapters.keys():
						rack = self.tiprack_adapters[slot][1].child
						for x,item in enumerate(self.adapter_pickup_tipracks[name]):
							if item == rack:
								pop_these_adapter_slots.append(x)
					else:
						rack = self.ctx.deck[slot]
						if slot in self.ex_slots:
							for x,item in enumerate(self.ex_racks[name]):
								if item == rack:
									pop_these_expansion_slots.append(x)
						else:
							for x,item in enumerate(self.tipracks[name]):
								if item == rack:
									pop_these_active_deck.append(x)
					if rack in self.ctx.loaded_modules.values():
						continue #prevent things on the stacker from being moved incorrectly when nothing is not the third column
					labware_to_move.append(rack)
				pop_these_active_deck.sort(reverse=True)
				pop_these_expansion_slots.sort(reverse=True)
				pop_these_adapter_slots.sort(reverse=True)
				for labware in labware_to_move:
					self.ctx._core.move_labware(
						labware_core=labware._core,
						new_location=toss_location,
						use_gripper=toss_tips,
						pause_for_manual_move=False,
						pick_up_offset=(0.0,0.0,0.0),
						drop_offset=(0.0,0.0,0.0))
				for x in pop_these_active_deck:
					self.tipracks[name].pop(x)
				for x in pop_these_expansion_slots:
					self.ex_racks[name].pop(x)
				for x in pop_these_adapter_slots:
					self.adapter_pickup_tipracks[name].pop(x)
			else:
				raise KeyError(f"Tiprack {name} not found in tiprack list")
			

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


	def move_from_stacker(self,rackname : str) -> protocol_api.Labware:
		'''
		Grab the next tiprack available from the any stacker module that has tipracks of the given rackname. If a tiprack has a lid, it will be removed and placed in the waste bin. If the stacker has the proper racktype on the shuttle, it will return the shuttle labware. If the stacker has a different labware on it, it will move it to the open_slot first. \
		Returns the labware object retrieved from the stacker. It will use all of the tipracks in one stacker before moving to the next stacker of the same labware type
		
		:param self: TipTracker object
		:param rackname: The API load name of the labware (tiprack) to retrieve from the stacker
		:type rackname: str
		:return: Returns the next available labware from the stacker of the given rackname
		:rtype: Labware
		'''
		stacker = None
		chosen_index = None
		for x,stacker_list in enumerate(self.stackers[rackname]):
			if stacker_list[1] > 0:
				stacker : protocol_api.FlexStackerContext = stacker_list[0]
				chosen_index = x
				break
		if stacker is None:
			raise ValueError(f"No tipracks remaining in stackers for {rackname}")
		stacker_current_labware = stacker.labware
		if stacker_current_labware == None:
			if self.print_comments:
				self.ctx.comment(f'Retrieving labware from stacker for {rackname}')
			if self.debug:
				print(f'Retrieving labware from stacker for {rackname}')
			labware = stacker.retrieve()
			self.stackers[rackname][chosen_index][1] = self.stackers[rackname][chosen_index][1] - 1 #Change Quantity of stacker
			if self.stackers[rackname][chosen_index][2]: #This should be changes, internal flag for lid
				if self.print_comments:
					self.ctx.comment(f'Removing lid from stacker for {rackname}')
				if self.debug:
					print(f'Removing lid from stacker for {rackname}')
				self.ctx.move_lid(labware,self.waste,use_gripper=self.use_gripper)
		
		else:
			if stacker_current_labware.load_name != rackname:
				if self.print_comments:
					self.ctx.comment(f'Labware on stacker is not {rackname}, moving to {self.open_slot} and retrieving new labware')
				if self.debug:
					print(f'Labware on stacker is not {rackname}, moving to {self.open_slot} and retrieving new labware')
				if self.open_slot == None:
					raise ValueError("No open slot defined, please define an open slot to move the labware to with non-matching labware on shuttle")
				self._shuttle_labware(stacker_current_labware,self.open_slot)
				self.return_to_stacker = (stacker_current_labware,self.open_slot,rackname,chosen_index)
				labware = stacker.retrieve()
				self.stackers[rackname][chosen_index][1] = self.stackers[rackname][chosen_index][1] - 1 #Change Quantity of stacker
				if self.stackers[rackname][chosen_index][2]: 
					if self.print_comments:
						self.ctx.comment(f'Removing lid from stacker for {rackname}')
					if self.debug:
						print(f'Removing lid from stacker for {rackname}')
					self.ctx.move_lid(labware,self.waste,use_gripper=self.use_gripper)
			else:
				if self.print_comments:
					self.ctx.comment(f'Getting labware already on shuttle for {rackname}')
				if self.debug:
					print(f'Getting labware already on shuttle for {rackname}')
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
		
	
	def add_stacker(self, slot : str, rackname : str, initial_count : int, lid : str | None, load_on_shuttle : bool = True, use_for_storing_empty : bool = False) -> protocol_api.FlexStackerContext:
		'''
		Load a stacker module on the deck and fill it with an initial number of tipracks. This replaces using ctx.load_module() directly to ensure proper tracking of the stacker and its contents.\
		The function will return a FlexStackerContext object that can be used as normal.
		
		:param self: TipTracker object
		:param slot: The deck slot to load the stacker module on
		:type slot: str
		:param rackname: The API load name of the labware (tiprack) to load into the stacker
		:type rackname: str
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
			self.ctx.comment(f'Adding stacker module on slot {slot} with {initial_count} {rackname}')
		if self.debug:
			print(f'Adding stacker module on slot {slot} with {initial_count} {rackname}')
		stacker_obj = self.ctx.load_module('flexStackerModuleV1', slot)
		if rackname in self.stackers.keys():
			self.stackers[rackname].append([stacker_obj,None,True if lid != None else False])
		else:
			self.stackers[rackname] = [[stacker_obj,None,True if lid != None else False]]
		if not use_for_storing_empty:
			self.load_tips_in_stacker(stacker_obj,rackname,initial_count,lid,load_on_shuttle)
		else:
			if self.print_comments:
				self.ctx.comment(f'Using stacker on slot {slot} for storing empty tipracks, setting carousel to true')
			if self.debug:
				print(f'Using stacker on slot {slot} for storing empty tipracks, setting carousel to true')
			if self.carousel_tips == False:
				self.carousel_tips = True
				self.open_slot = stacker_obj
			if rackname not in self.storing_stackers.keys():
				self.storing_stackers[rackname] = [[stacker_obj,0]]
			else:
				self.storing_stackers[rackname].append([stacker_obj,0])
		return stacker_obj


	def load_tips_in_stacker(self,stacker : protocol_api.FlexStackerContext,rackname : str,quantity : int,lid : str | None = None, load_on_shuttle : bool = True) -> None:
		'''
		Function to load tipracks in stacker at the beginning of the protocol. Currently also used to reload stackers when they run out of tips, but will change this in the future.
		
		:param self: TipTracker object
		:param stacker: The FlexStackerContext to load tips into
		:type stacker: protocol_api.FlexStackerContext
		:param rackname: The API load name of the labware (tiprack) to load into the stacker
		:type rackname: str
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
			self.ctx.comment(f'Loading {quantity} {rackname} into stacker in {stacker}')
		if self.debug:
			print(f'Loading {quantity} {rackname} into stacker in {stacker}')
		stacker.set_stored_labware(rackname,count=quantity - 1 if load_on_shuttle else quantity,lid=lid)
		if rackname not in self.tip_rack_counts.keys():
			self.tip_rack_counts[rackname] = quantity
		else:
			self.tip_rack_counts[rackname] = self.tip_rack_counts[rackname] + quantity
		if load_on_shuttle:
			if self.print_comments:
				self.ctx.comment(f'Loading labware onto stacker shuttle for {rackname}')
			if self.debug:
				print(f'Loading labware onto stacker shuttle for {rackname}')
			stacker.load_labware(rackname)
		for x,stacker_list in enumerate(self.stackers[rackname]):
			if stacker_list[0] == stacker:
				self.stackers[rackname][x][1] = quantity - 1 if load_on_shuttle else quantity


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
	
	def _refill_deck_manually(self,slots : list[str],rackname : str) -> None:
		'''
		Internal function to refill tipracks manually by prompting the user to place new racks on the deck and then assigning them to the pipettes. This is used when not using the waste chute or gripper to move racks around, so the user has to manually move racks on and off the deck. This function will be called after prompting the user to remove old racks with clear_old() if not using the waste chute, and then will prompt the user to place new racks on the deck in the specified slots before assigning those slots to the given rackname and assigning that rackname to the pipettes.
		
		:param self: TipTracker object
		:param slots: The slots where the new tipracks have been placed by the user
		:type slots: list[str]
		:param rackname: The API load name of the tiprack that has been placed on the deck
		:type rackname: str
		:return: None
		:rtype: None
		'''
		if self.print_comments:
			self.ctx.comment(f'Please place new {rackname} tipracks on deck in slots {slots}')
		if self.debug:
			print(f'Please place new {rackname} tipracks on deck in slots {slots}')
		self.ctx.pause(f'Please place new {rackname} tipracks on deck in slots {slots}')
		self.reset_rack_list(rackname)
		self.refill_tips(rackname,slots,waste_all_old=False)
		self.reset_rack_list(rackname)
	
	
	def _refill_stacker_manually(self,rack_name : str, empty_tipracks : list[str] = []) -> None:
		'''
		Internal function to facilitate the refilling of stackers using a pause method, one pause per stacker
		
		:param self: TipTracker object
		:param rack_name: The API load name of the tiprack to refill for all empty stackers
		:type rack_name: str
		:param empty_tipracks: The list of empty tiprack slots to be refilled with the stacker tipracks after refilling the stackers
		:type empty_tipracks: list[str]
		'''
		if self.print_comments:
			self.ctx.comment('No remaining tipracks in stackers, manual refill needed')
		if self.debug:
			print('No remaining tipracks in stackers, manual refill needed')
		for x,stacker in enumerate(self.stackers[rack_name]):
			count_to_load = 6 if self.max_racks_count.get(rack_name,None) == None else min(6,self.max_racks_count[rack_name]-self.tip_rack_counts.get(rack_name,0))
			self.stackers[rack_name][x][1] = count_to_load
			self.stackers[rack_name][x][0].fill(count_to_load)
		#Load only the last racks in the stacker and move them over instead of having a user place racks in the stacker and on the deck
		if self.max_racks_count.get(rack_name,None) == self.tip_rack_counts.get(rack_name,-1):
			self.call_refill = False
			if self.print_comments:
				self.ctx.comment(f'Max racks of {rack_name} reached, last racks in stacker')
			if self.debug:
				print(f'Max racks of {rack_name} reached, last racks in stacker')
			for empty_slot in empty_tipracks[:count_to_load]:
				next_rack = self.move_from_stacker(rack_name)
				self._shuttle_labware(next_rack,empty_slot)
	
	
	def grab_from_stacker(self,rack_name : str, empty_slots : list[str] = []) -> None:
		'''
		Grab the next tiprack available from the stacker for the given rack type. If a tiprack has a lid, it will be removed and placed in the waste bin. If the stacker has the proper racktype on the shuttle, it will return the shuttle labware. If the stacker has a different labware on it, it will move it to the open_slot first. \
		Moves labware onto the deck; does not return the labware object to the caller.
		
		:param self: TipTracker object
		:param rack_name: The API load name of the labware (tiprack) to retrieve from the stacker
		:type rack_name: str
		:return: None
		:rtype: None
		'''
		if not self.carousel_tips:
			for slot in empty_slots:
				for stacker in self.stackers[rack_name]:
					if stacker[1] > 0:
						if self.print_comments:
							self.ctx.comment(f'Tiprack in {stacker[0]}, moving to {slot}')
						if self.debug:
							print(f'Tiprack in {stacker[0]}, moving to {slot}')
						next_rack = self.move_from_stacker(rack_name)
						self._shuttle_labware(next_rack,slot)
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
				for stacker in self.stackers[rack_name]:
					if stacker[1] > 0:
						next_rack = self.move_from_stacker(rack_name)
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
6. Began work to remove all refill calls into their own functions for easily tracking and flexibility
'''

