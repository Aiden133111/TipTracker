from opentrons import protocol_api
from opentrons.protocol_api.labware import OutOfTipsError
from opentrons.protocol_api import ALL, COLUMN

#PROTOCOL REQUIREMENTS
metadata = {
	'protocolName': 'Tip Tracking Class',
	'author': 'Aiden McFadden, Opentrons',
	'source': 'Custom Protocol Development',
	'description' : 'Goal is to create a method to track tips across a run in a flexable fasion',
}

requirements = {
	"robotType": "Flex",
	"apiLevel": "2.22",
}
#########################
'''
FEATURES TO IMPLEMENT
1. Add tiprack limits for each type of tip 
2. Edge case for error recovery on last available tip
'''
##########################
class TipTracker:
	'''Create a tip tracking object to easily facitate how protocols that require many tips should have them added to the deck. \
		Will pause the protocol to refill tips when empty. Or will move extra tipracks from expansion slots to the active deck when out \
		Can also dispose of emopty racks through with the gripper and waste chute if both are connected to the robot.
		Track_object = TipTracker(ctx, pipette1, pipette2, waste_bin, use_gripper=False)
		ctx = protocol_api.ProtocolContext , your protocol context to access protocol information
		pipette1 = protocol_api.InstrumentContext , your first pipette
		pipette2 = protocol_api.InstrumentContext , your second pipette 
		waste_bin = protocol_api.WasteChute or protocol_api.TrashBin , the waste being used
		use_gripper = bool, if True will use the gripper to move labware, if False will use the manual method of moving labware off and on deck. \
		'''
	#Off deck type name as str OffDeckType.OFF_DECK

	def __init__(self, ctx : protocol_api.ProtocolContext, pipette1 : protocol_api.InstrumentContext, pipette2 : protocol_api.InstrumentContext, waste_bin : protocol_api.WasteChute | protocol_api.TrashBin, use_gripper : bool = False, debugging : bool = False):

		self.ctx : protocol_api.ProtocolContext = ctx													#ProtocolContext
		self.debug : bool = debugging																	#Debugging mode flag
		self.pipette1 : protocol_api.InstrumentContext = pipette1										#First pipette
		self.pipette2 : protocol_api.InstrumentContext | None = pipette2								#Second Pipette
		self.ex_slots : list[str] | None = None															#If using expansion slots
		self.use_gripper : bool = use_gripper															#If using gripper
		self.waste : protocol_api.WasteChute | protocol_api.TrashBin = waste_bin						#The waste bin type to use
		self.tipracks : dict[protocol_api.Labware.load_name : list[str]] = {}							#Active deck tiprack tracker, internal strings are deck slots
		self.ex_racks : dict[protocol_api.Labware.load_name : list[str]] = {}							#Expansion slots tiprack tracker
		self.empty_ex_slots : dict[protocol_api.Labware.load_name : list[str]] = {}						#Dictionary of empty expansion slots that previously had tips
		self.rack_assignments : dict[protocol_api.Labware.load_name : list[str]] = {}					#Dictionary map of where tipracks should be loaded
		self.tip_counts : dict[protocol_api.Labware.load_name : int] = {}								#Dictionary of # of used tips for each rack type 
		self.tip_rack_counts : dict[protocol_api.Labware.load_name : int] = {}							#Dictionary of tipracks loaded for each rack type
		self.open_slot : str | None = None																#Slot with nothing on it, placeholder slot for carousel
		self.original_open_slot : str | None = None														#Origional open_slot for carousel
		self._using_stackers : bool= False																#Internal property if stackers are being used
		self.stackers : dict[protocol_api.Labware.load_name : list[protocol_api.ModuleContext,int]] = {}#Dictionary of stacker instrument context and number of racks in the stacker, key is rack load name
		self.use_chute : bool = True if type(waste_bin) == protocol_api.WasteChute else False			#Use waste chute to dispose of tips if present 
		self.carousel_tips : bool = False if type(waste_bin) == protocol_api.WasteChute else True		#Carosuel tips if no waste chute
		self.pick_up_count : dict[protocol_api.InstrumentContext : int] = {pipette1 : 0, pipette2 : 0} 	#How many time pick up tip has been called for each pipette
		self.drop_count : dict[protocol_api.InstrumentContext : int] = {pipette1 : 0, pipette2 : 0}		#How many time drop tip has been called for each pipette


	def assign_slots(self, tiprack1 : str, slots1 : str | list[str], tiprack2 : str = None,slots2 : list[str] | str = None, tiprack3 : str = None, slots3 : str | list[str] = None):
		'''Dedicate slots to a tiprack, this is used as the slots to refill racks on the deck when they are out. \
			Use this method when the slots you want tips to be reloaded on are different than the slots they started on. \
			This is currently UNTESTED to reuse during a protocol. Can take three tipracks-slot pairs at once.
			tiprack1 = str of the tiprack load name,
			slots1 = list of slots to load tiprack1 onto, can be str or list of strings
			tiprack2 = str of the tiprack load name,
			slots2 = list of slots to load tiprack2 onto, can be str or list of strings
			tiprack3 = str of the tiprack load name,
			slots3 = list of slots to load tiprack3 onto, can be str or list of strings'''
		for tiprack,slots in zip([tiprack1, tiprack2, tiprack3],[slots1, slots2, slots3]):
			if tiprack == None and slots == None:
				continue
			if type(slots) == str:
				slots = [slots]
			self.rack_assignments[tiprack] = slots

	def load_tipracks(self, tiprack1 : str, slots1 : str | list[str], tiprack2 : str = None,slots2 : list[str] | str = None, tiprack3 : str = None, slots3 : str | list[str] = None):
		'''Add tipracks to the deck and to the internal data. This method should always be used to load tipracks onto the deck to change decklayout. \
			Use starting tipracks to intially load the deck to also assign the same slots to the tipracks\
			Although it is done manually through the code when more tipracks are needed, it can be used independently to load tipracks onto the deck. \
			Can take three tipracks-slot pairs at once. This done initially when calling starting_tipracks.
			tiprack1 = str of the tiprack load name,
			slots1 = list of slots to load tiprack1 onto, can be str or list of strings
			tiprack2 = str of the tiprack load name,
			slots2 = list of slots to load tiprack2 onto, can be str or list of strings
			tiprack3 = str of the tiprack load name,
			slots3 = list of slots to load tiprack3 onto, can be str or list of strings'''
		for slots in [slots1, slots2, slots3]:
			if type(slots) == str:
				slots = [slots]

		#Load labware for each tiprack in each slot
		for rackname,slots in zip([tiprack1, tiprack2, tiprack3],[slots1, slots2, slots3]):
			if rackname != None:
				for slot in slots:
					if type(slot) == str:
						rack = self.ctx.load_labware(rackname, slot)
						if rackname not in self.tip_rack_counts.keys():
							self.tip_rack_counts[rackname] = 1
						else:
							self.tip_rack_counts[rackname] = self.tip_rack_counts[rackname] + 1
					else:
						raise TypeError('This class cannot handle tipracks on adapter. 96 channel tip tracking not supported')
					if self.ex_slots != None and slot in self.ex_slots:
						if rackname in self.ex_racks.keys():
							self.ex_racks[rackname].append(rack)
						else:
							self.ex_racks[rackname] = [rack]
					else:
						if rackname in self.tipracks.keys():
							self.tipracks[rackname].append(rack)
						else:
							self.tipracks[rackname] = [rack]

	def pick_up(self, pipette : int | str | protocol_api.InstrumentContext, locus : protocol_api.Labware | protocol_api.Well | None = None, refill_all : bool = False) -> int:
		'''Main use of the tracker function. If we run out of tips using this method, instead of an error being thrown it will check for extra racks in expansion slots \
		or prompt users to phyically refill the tips. It will use the waste chute to throw out the empty tip racks before it needs to refill.
		pipette = Any of the following (1,'One','one','1') or (2,'2',self.pipette2,'two','Two') to indicate which pipette to use
		locus = optional Labware or Well to use to pick up tip, for example reuse tips
		refill_all = bool, if True will refill all other empty racks with tips when out of the needed tip, if False will only refill the assigned tipracks that are out
		
		Returns Integer corresponding to the following:
		0 - Pickup, succesful pickup, no swap needed
		1 - Had to carosuel to pickup tip
		2 - Wasted Tip, Grabbed from expansion
		3 - Wasted Tip, Grabbed from stacker
		4 - Manual Refill started
		'''
		#Assign proper pipette and check what tips are currently assigned
		pip = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
		if pip == None:
			raise ValueError(f"Invalid pipette: {pipette}, must be in [1,'1',self.pipette1,'one','One'] or [2,'2',self.pipette2,'two','Two']")
		self.pick_up_count[pip] = self.pick_up_count[pip] + 1
		#update tiprack list if deck has changed since last pick up
		rack_name = pip.tip_racks[0].load_name
		pip.tip_racks = self.tipracks[rack_name]
		old_rack_slots = [slot for slot in self.rack_assignments[rack_name]] # Get the slots that are not expansion slots
		waste_slots = [slot for slot in old_rack_slots if slot not in self.ex_slots]
		#Add rack slots to a dictionary IFF they have no tips
		other_rack_slots = { rack_load_name : [rack.parent for rack in rack_list if not any([well.has_tip for well in rack.wells()])] for rack_load_name,rack_list in self.tipracks.items() if rack_load_name != rack_name} # Move these to waste
		empty_tip_slots = {rack_load_name : [slot for slot in racklist if self.ctx.deck[slot] == None] for rack_load_name, racklist in self.rack_assignments.items()} # Load these plus other racks slots
		
		if self.open_slot != None and self.original_open_slot == None:
			self.original_open_slot = self.open_slot
		#Try and pick up tip
		try:	
			pip.pick_up_tip(locus)
			return 0
		except OutOfTipsError:
			self.ctx.comment('Out of tips, starting refilling process')
			if self.debug:
				print('Out of tips, starting refilling process')
			#Trash old tips
			if not self.carousel_tips: #Trash tips in waste chute if able
				for slot in waste_slots:
					if self.ctx.deck[slot] != None and self.ctx.deck[slot].load_name == rack_name:
						self.waste_tips(slot)
			#If out of tips and no expansions, refill tips of the same size
			self.ctx.comment('Out of tips, starting refilling process')
			if self.debug:
				print('Out of tips, starting refilling process')
			if self.ex_slots == None and self._using_stackers == False:
				self.ctx.comment('No expansion slots defined, Refilling Manually') # Dont have to worry about carousel here, no ex slots
				if self.debug:
					print('No expansion slots defined, Refilling Manually')
				self.refill_tips(rack_name,old_rack_slots)
				self.ctx.home()
				self.ctx.pause(f"Please place {rack_name} onto slots {old_rack_slots}")
				self.assign_tipracks(pipette,rack_name)
				#Optionally refill all used tip racks, dont think this counts expansion deck slots
				if refill_all:
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
				pip.pick_up_tip(locus)
				return 4
			else:				
				self.ctx.comment('Expansion slots or stackers defined, starting refilling process')
				if self.debug:
					print('Expansion slots defined, starting refilling process')
				if refill_all:
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
					self.ctx.comment('Tiprack on expansion slot, moving to active deck')
					if self.debug:
						print('Tiprack on expansion slot, moving to active deck')
					if self.carousel_tips:
						for old_rack,e_rack in zip(self.tipracks[rack_name],self.ex_racks[rack_name]):
							self.carousel(old_rack,e_rack)
							return_code = 1
					else:
						for e_rack, open_slot in zip(self.ex_racks[rack_name],waste_slots): #This needs a check for if expansion slot has tips 
							e_slot_source = e_rack.parent
							self.ctx.move_labware(e_rack, open_slot,use_gripper=self.use_gripper)
							if rack_name in self.empty_ex_slots.keys():
								self.empty_ex_slots[rack_name].append(e_slot_source)
							else:
								self.empty_ex_slots[rack_name] = [e_slot_source]
							return_code = 2
					self.reset_rack_list(rack_name)			
					self.assign_tipracks(pipette,rack_name)
					
					pip.pick_up_tip(locus)
					return return_code
				elif rack_name in self.stackers.keys() and self.stackers[rack_name][1] > 0:
					self.ctx.comment('Tiprack in stacker, moving to active deck')
					if self.debug:
						print('Tiprack in stacker, moving to active deck')
					next_rack = self.move_from_stacker(rack_name)
					self._shuttle_labware(next_rack,empty_tip_slots[rack_name])
					self.reset_rack_list(rack_name)
					self.assign_tipracks(pipette,rack_name)
					pipette.pick_up_tip(locus)
					return 3
				else:
					if self.ex_racks:
						self.ctx.comment('No remaining tipracks on expansion deck, manual refill needed')
						if self.debug:
							print('No remaining tipracks on expansion deck, manual refill needed')
					elif self._using_stackers:
						self.ctx.comment('No remaining tipracks in stackers, manual refill needed')
						if self.debug:
							print('No remaining tipracks in stackers, manual refill needed')
						self.stackers[rack_name].fill(count=7)
						self.stackers[rack_name][1] = 7
					self.ctx.home()
					self.refill_tips(rack_name,self.rack_assignments[rack_name])
					self.reset_rack_list(rack_name)
					self.assign_tipracks(pipette,rack_name)
					self.open_slot = self.original_open_slot

					pip.pick_up_tip(locus)
					return 4
					
					#Pause protocol and prompt user to load new tipracks, could we have option to add all tipracks

		if rack_name in self.tip_counts.keys():
			self.tip_counts[rack_name] = self.tip_counts[rack_name] + pip.active_channels
		else:
			self.tip_counts[rack_name] = pip.active_channels

	def add_starting_tipracks(self, tiprack1 : str, slots1 : str | list[str], tiprack2 : str = None,slots2 : list[str] | str = None, tiprack3 : str = None, slots3 : str | list[str] = None):
		'''Load tipracks onto the deck and assign the proper slots to reload them onto. This method should always be used to the first set of tipracks just to ensure they properly \
			match, but these variables could be different. i.e. you do not want the tipracks to be refilled onto the same slots as they start on. Can take three tipracks-slot pairs at once.
			tiprack1 = str of the tiprack load name,
			slots1 = list of slots to load tiprack1 onto, can be str or list of strings
			tiprack2 = str of the tiprack load name,
			slots2 = list of slots to load tiprack2 onto, can be str or list of strings
			tiprack3 = str of the tiprack load name,
			slots3 = list of slots to load tiprack3 onto, can be str or list of strings'''
		assign_slots = [slots1, slots2, slots3]
		tipracks = [tiprack1, tiprack2, tiprack3]
		for slot, rack in zip(assign_slots,tipracks):
			if slot != None and rack != None:
				continue
			elif slot == None and rack == None:
				continue
			else:
				raise ValueError(f"Tiprack {rack} and slots {slot} must be defined together")
		
		self.load_tipracks(tiprack1,slots1,tiprack2,slots2,tiprack3,slots3)
		self.assign_slots(tiprack1,slots1,tiprack2,slots2,tiprack3,slots3)

	def reset_rack_list(self,rack_name):
		'''Fetches a rackname and resets its internal data for the type of rack. Can be useful when you move the deck around with the gripper, \
		place new racks on the deck or even perform manual moves of labware 
		rack_name = str of the rack load name to reset, i.e. opentrons_flex_96_tiprack_50ul'''
		rack_list = []
		ex_list = []
		for slot,item in self.ctx.deck.items(): 
			if not item or item in self.ctx.loaded_modules.values():
				continue
			if item.load_name == 'opentrons_flex_96_tiprack_adapter':
				rack_obj = item.child
			else:
				rack_obj = item
			if rack_obj.load_name == rack_name:
				if slot in self.ex_slots:
					ex_list.append(rack_obj)
				else:
					rack_list.append(rack_obj)
		self.tipracks[rack_name] = rack_list
		self.ex_racks[rack_name] = ex_list


	def add_expansion_slots(self, slots):
		'''Add expansion slots to the available slots on the deck.
		 This will not be used unless you assign them to a tiprack as well to load tipracks onto  the deck slot
		 slots = ['A4','B4','C4','D4'] as list of strings or 'A4' as string \
		'''
		if self.ex_slots == None:
			if type(slots) == str:
				self.ex_slots = [slots]
			elif type(slots) == list:
				self.ex_slots = slots
			else:
				raise TypeError("Expansion slots must be a string or list of strings")
		else:
			if type(slots) == str:
				self.ex_slots.append(slots)
			elif type(slots) == list:
				self.ex_slots.extend(slots)
			else:
				raise TypeError("Expansion slots must be a string or list of strings")
		self.ex_slots = list(set(self.ex_slots))
		invalid_slots = [x for x in self.ex_slots if x not in ['A4','B4','C4','D4']]
		if len(invalid_slots) > 0:
			raise ValueError(f"Invalid expansion slots: {invalid_slots}, slots must be A4, B4, C4, or D4")
			
	def drop_tip(self, pipette : int | str | protocol_api.InstrumentContext, locus : protocol_api.Labware | protocol_api.Well | None = None, return_tip : bool = False):
		'''Drop tip at locus, if locus is None will drop tip at the default waste bin if dropping or back to its original slot if returning. 
		pipette = 1 or 2, corresponding to which order you loaded them in
		locus = labware or well to drop tip at, if None will drop at default waste bin
		return_tip = bool, if True will return tip to original slot instead of dropping it at the waste bin'''
		pip = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
		if pip == None:
			raise ValueError(f"Invalid pipette number {pipette}, must be 1 or 2, as strings or integers or pipette objects")
		self.drop_count[pip] = self.drop_tip[pip] + 1
		if return_tip:
			pip.return_tip(locus)
		else:
			pip.drop_tip(locus)

			
	def replace_tips(self,old_rack_name : str, new_rack_name : str , number_to_replace : int | None = None, manually_remove = True):
		'''Remove a certain number (or all) of a certain type of tiprack to replace with a new type. \
		Useful when you no longer need a type of tip on deck and you want the space for something else.
		old_racks = list of tiprack labware objects to replace, can be a list of labware or a single labware object
		new_rack_name = str of the new tiprack load name
		number_to_replace = int of how many to replace, if None will replace all of that type'''
		self.ctx.comment(f'Replacing {number_to_replace} {old_rack_name} with {new_rack_name}')
		if self.debug:
			print('Replacing {number_to_replace} {old_rack_name} with {new_rack_name}')
		slot_list = self.rack_assignments[old_rack_name][:number_to_replace]
		self.ctx.comment('Replacing tipracks')
		if self.debug:
			print('Replacing Tipracks')
		self.ctx.home()
		self.clear_old(old_rack_name,slot_list,manually_remove)
		new_rack_slot_list = self.rack_assignments[new_rack_name].extend(slot_list)
		old_rack_slot_list = [] if number_to_replace == None else self.rack_assignments[old_rack_name][self.rack_assignments[old_rack_name][number_to_replace:]]
		#Option to remove the old assignment 
		self.assign_slots(tiprack1=new_rack_name,slots1=new_rack_slot_list,
						tiprack2=old_rack_name,slots2=old_rack_slot_list)
		self.load_tipracks(new_rack_name,slot_list)


	def refill_tips(self, name, slots):
		'''Internal Function to facilitate refilling tips of the same size. First clears the old data then replaces it by loading fresh tip boxes. \
		Can call this method manually instead of calling clear_old load_tipracks independently. \
		name = tiprack load name as str
		slots = list of slots to refill, if str or labware will be converted to list. Labware should be passed only if it is a tiprack adapter'''
		self.ctx.comment(f'Refilling tips of {name} on {slots}')
		if self.debug:
			print(f'Refilling tips of {name} on {slots}')
		self.clear_old(name)
		self.load_tipracks(name,slots)

	def waste_tips(self, slots):
		'''Move tipboxes to waste, this is done automatically when a all types of a tip are used or when refill_all=True for tip pickup but can be used to manually move tips from any slots to waste. \
		If called manually, make sure to use clear_old on the slots after to remove it from internal data \
		slots = list of slots to move to waste, if str or labware will be converted to list. Labware should be passed only if it is a tiprack adapter'''
		self.ctx.comment(f'Wasting tips on slots {slots}: Using gripper : {self.use_gripper}')
		if self.debug:
			print(f'Wasting tips on slots {slots}: Using gripper : {self.use_gripper}')
		if type(slots) == str or type(slots) == protocol_api.Labware:
			slots = [slots]
		if self.use_chute and self.use_gripper:
			for rack in slots:
				self.ctx.move_labware(self.ctx.deck[rack], self.waste,use_gripper=self.use_gripper)
		else:
			for rack in slots:
				self.ctx.move_labware(self.ctx.deck[rack], protocol_api.OFF_DECK)

	def assign_tipracks(self, pipette : int | str | protocol_api.InstrumentContext, name : str):
		'''Assign tipracks to pipette, this is done automatically when loading tips but can be used to reassign if needed.\
		Instead of pip.tip_racks = [tipracks], use trackerObj.assign_tipracks(1,opentrons_flex_96_filtertip_50ul).\
		pipette = 1 or 2, corresponding to which order you loaded them in
		name = tiprack load name as str'''
		self.ctx.comment(f'Reassigning tipracks of {pipette} to {name}')
		if self.debug:
			print(f'Reassigning tipracks of {pipette} to {name}')
		pip = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
		if pip == None:
			raise ValueError(f"Invalid pipette number {pipette}, must be 1 or 2")
		pip.tip_racks = self.tipracks[name]

	def clear_old(self,name : str ,slots_to_clear : None | list = None,save_tips = True):
		'''Remove old tipracks from internal data to replace with new tipracks in another function. This should generally only be used internally.\
		Only use if you are sure you want to remove the tipracks from the internal data without moving them off deck physically. Keeps protocol from trying to move labware not on the deck anymore
		name = Tiprack load name
		slots_to_clear = List of slots to clear, if None will clear all tipracks of that type'''
		self.ctx.comment(f'Clearing old tipracks of {name}')
		if self.debug:
			print(f'Clearing old tipracks of {name}')
		if save_tips == False and self.use_chute == True and self.use_gripper == True:
			self.ctx.comment('Using Gripper to remove tips')
			if self.debug:
				print('Using Gripper to remove tips')
			toss_tips = True
			toss_location = self.waste
		else:
			slots_message = 'All slots' if slots_to_clear == None else str(slots_to_clear)
			self.ctx.pause(f'Please remove all {name} from {slots_message}')
			toss_tips = False
			toss_location = protocol_api.OFF_DECK
		if slots_to_clear == None:
			if name in self.rack_assignments.keys():
				for rack in self.tipracks[name]:
					self.ctx._core.move_labware(
						labware_core=rack._core,
						new_location=toss_location,
						use_gripper=toss_tips,
						pause_for_manual_move=False,
						pick_up_offset=(0.0,0.0,0.0),
						drop_offset=(0.0,0.0,0.0))
				self.tipracks[name] = []
			else:
				raise KeyError(f"Tiprack {name} not found in tiprack list")
		else:
			pop_these_active_deck = []
			pop_these_expansion_slots = []
			if name in self.tipracks.keys():
				for x,rack in enumerate(self.tipracks[name]):
					if type(rack.parent) == str:
						slot_check = rack.parent
					else:
						slot_check = rack.parent.parent
					if slot_check in slots_to_clear:
						pop_these_active_deck.append(x)
				pop_these_active_deck.sort(reverse=True)
				for x in pop_these_active_deck:
					self.ctx._core.move_labware(
						labware_core=self.tipracks[name][x]._core,
						new_location=toss_location,
						use_gripper=toss_tips,
						pause_for_manual_move=False,
						pick_up_offset=(0.0,0.0,0.0),
						drop_offset=(0.0,0.0,0.0))
					self.tipracks[name].pop(x)
				for x,rack in enumerate(self.ex_racks[name]):
					if type(rack.parent) == str:
						slot_check = rack.parent
					else:
						slot_check = rack.parent.parent
					
					if slot_check in slots_to_clear:
						pop_these_expansion_slots.append(x)
				pop_these_expansion_slots.sort(reverse=True)
				for x in pop_these_expansion_slots:
					self.ctx._core.move_labware(
						labware_core=self.ex_racks[name][x]._core,
						new_location=toss_location,
						use_gripper=toss_tips,
						pause_for_manual_move=False,
						pick_up_offset=(0.0,0.0,0.0),
						drop_offset=(0.0,0.0,0.0))
					self.ex_racks[name].pop(x)
			else:
				raise KeyError(f"Tiprack {name} not found in tiprack list")
			
	def carousel(self, tiprack_to_move_away : protocol_api.Labware | str,tiprack_to_move_in : protocol_api.Labware | str):
		
		if self.open_slot != None:
			open_slot = self.open_slot
		else:
			raise ValueError("No open slot defined, please define an open slot to move the tiprack to")
		
		if type(tiprack_to_move_away) == str:
			intermediate_slot = tiprack_to_move_away
			tiprack_to_move_away = self.ctx.deck[intermediate_slot]
		elif type(tiprack_to_move_away) == protocol_api.Labware:
			intermediate_slot = tiprack_to_move_away.parent
		
		if type(tiprack_to_move_in) == str:
			leaving_open_slot = tiprack_to_move_in
			tiprack_to_move_in = self.ctx.deck[leaving_open_slot]
		elif type(tiprack_to_move_in) == protocol_api.Labware:
			leaving_open_slot = tiprack_to_move_in.parent
		
		
		if self.debug:
			print(f' Carousel from {tiprack_to_move_away} on {intermediate_slot} to {self.open_slot}')
		self._shuttle_labware(tiprack_to_move_away,open_slot)
		if self.debug:
			print(f' Carousel from {tiprack_to_move_in} on {leaving_open_slot} to {intermediate_slot}')
		self._shuttle_labware(tiprack_to_move_in,intermediate_slot)
		if self.debug:
			print(f'----->Assigning open_slot to {leaving_open_slot}')
		self.open_slot = leaving_open_slot
				
	def move_from_stacker(self,rackname,):
		stacker = self.stackers[rackname][0]
		self.stackers[rackname][1] = self.stackers[rackname][1] - 1 #Change Quantity of stacker
		labware = stacker.retreive()
		if labware.child != None: #Throw away lid 
			self.ctx.move_labware(labware.child,self.waste,use_gripper=self.use_gripper)
		return labware

	def load_tips_in_stacker(self,stacker,rackname,quantity,lid : bool = False):
		self._using_stackers == True
		stacker.set_stored_labware(load_name=rackname,count=quantity,lid=lid)
		if rackname not in self.tip_rack_counts.keys():
			self.tip_rack_counts[rackname] = quantity
		else:
			self.tip_rack_counts[rackname] = self.tip_rack_counts[rackname] + quantity
		self.stackers[rackname] = [stacker,quantity]

	def _shuttle_labware(self,labware,location):
		self.ctx.move_labware(labware,location,use_gripper=self.use_gripper)