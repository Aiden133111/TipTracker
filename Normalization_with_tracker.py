from opentrons import protocol_api
from opentrons import types
from opentrons.protocol_api.labware import OutOfTipsError
from math import ceil, floor, pi

#PROTOCOL REQUIREMENTS
metadata = {
	'protocolName': 'Post-Normalization Cleanup - AgriSeq',
	'author': 'Aiden McFadden, Opentrons',
	'source': 'Custom Protocol Development',
	'description' : 'Final Cleanup and pooling of samples post normalization PCR.'
}

requirements = {
	"robotType": "Flex",
	"apiLevel": "2.24",
}
	
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

	def __init__(self, ctx : protocol_api.ProtocolContext, pipette1 : protocol_api.InstrumentContext, pipette2 : protocol_api.InstrumentContext, waste_bin : protocol_api.WasteChute | protocol_api.TrashBin, use_gripper : bool = False, debugging : bool = False, suppress_comments : bool = False):

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
		self.carousel_tips : bool = False if type(waste_bin) == protocol_api.WasteChute else True		#Carousel tips if no waste chute
		self.pick_up_count : dict[protocol_api.InstrumentContext : int] = {pipette1 : 0, pipette2 : 0} 	#How many time pick up tip has been called for each pipette
		self.drop_count : dict[protocol_api.InstrumentContext : int] = {pipette1 : 0, pipette2 : 0}		#How many time drop tip has been called for each pipette
		self.print_comments : bool = not suppress_comments 												#If True, will print comments to the protocol log
		self.max_racks_count : dict = {}
		self.ignore_slots : list[str] = []


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
					if self.max_racks_count.get(rackname,None) != None:
						if self.max_racks_count[rackname] == self.tip_rack_counts.get(rackname,0):
							if self.print_comments:
								self.ctx.comment(f'Max racks of {rackname} reached, not loading more')
							if self.debug:
								print(f'Max racks of {rackname} reached, not loading more')
							continue
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
		0 - Just Pickup, succesful pickup, no swap needed
		1 - Had to carousel to pickup tip
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
			return_code =  0
		except OutOfTipsError:
			if self.print_comments:
				self.ctx.comment('Out of tips, starting refilling process')
			if self.debug:
				print('Out of tips, starting refilling process')
			#Trash old tips
			if not self.carousel_tips: #Trash tips in waste chute if able
				for slot in waste_slots:
					if self.ctx.deck[slot] != None and self.ctx.deck[slot].load_name == rack_name:
						self.waste_tips(slot)
			#If out of tips and no expansions, refill tips of the same size
			if self.ex_slots == None and self._using_stackers == False:
				if self.print_comments:
					self.ctx.comment('No expansion slots defined, Refilling Manually') # Dont have to worry about carousel here, no ex slots
				if self.debug:
					print('No expansion slots defined, Refilling Manually')
				self.refill_tips(rack_name,old_rack_slots)
				self.ctx.home()
				self.ctx.pause(f"Please place {rack_name} onto slots {old_rack_slots}")
				self.assign_tipracks(pipette,rack_name)
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
				pip.pick_up_tip(locus)
				return_code = 4
			else:
				if self.print_comments:			
					self.ctx.comment('Expansion slots or stackers defined, starting refilling process')
				if self.debug:
					print('Expansion slots defined, starting refilling process')
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
						swap = False
						for old_rack,e_rack in zip(self.tipracks[rack_name],self.ex_racks[rack_name]):
							if all([well.has_tip for well in e_rack.wells()]):
								self.carousel(old_rack,e_rack)
								return_code = 1
								swap = True
						if swap == False:
							if self.print_comments:
								self.ctx.comment('No Tipracks on Expansion Slots have tips, beginning refill process')
							if self.debug:
								print('No Tipracks on Expansion Slots have tips, beginning refill process')
							self.refill_tips(rack_name,self.rack_assignments[rack_name])
						return_code = 4

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
				elif rack_name in self.stackers.keys() and self.stackers[rack_name][1] > 0:
					if self.print_comments:
						self.ctx.comment('Tiprack in stacker, moving to active deck')
					if self.debug:
						print('Tiprack in stacker, moving to active deck')
					next_rack = self.move_from_stacker(rack_name)
					self._shuttle_labware(next_rack,empty_tip_slots[rack_name])
					self.reset_rack_list(rack_name)
					self.assign_tipracks(pipette,rack_name)
					pipette.pick_up_tip(locus)
					return_code = 3
				else:
					if self.ex_racks:
						if self.print_comments:
							self.ctx.comment('No remaining tipracks on expansion deck, manual refill needed')
						if self.debug:
							print('No remaining tipracks on expansion deck, manual refill needed')
					elif self._using_stackers:
						if self.print_comments:
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
					return_code =  4
					
					#Pause protocol and prompt user to load new tipracks, could we have option to add all tipracks

		if rack_name in self.tip_counts.keys():
			self.tip_counts[rack_name] = self.tip_counts[rack_name] + pip.active_channels
		else:
			self.tip_counts[rack_name] = pip.active_channels
		return return_code

	def add_starting_tipracks(self, tiprack1 : str, slots1 : str | list[str], tiprack2 : str = None,slots2 : list[str] | str = None, tiprack3 : str = None, slots3 : str | list[str] = None, max_racks_1 : int = None, max_racks_2 : int = None, max_racks_3 : int = None):
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
			
		for max_rack,tiprack in zip([max_racks_1, max_racks_2, max_racks_3],tipracks):
			if max_rack != None and type(max_rack) != int:
				raise TypeError(f"Max racks must be an integer, got {type(max_rack)}")
			else:
				if max_rack != None:
					self.max_racks_count[tiprack] = max_rack
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
		self.drop_count[pip] = self.drop_count[pip] + 1
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
		if self.print_comments:
			self.ctx.comment(f'Replacing {number_to_replace} {old_rack_name} with {new_rack_name}')
		if self.debug:
			print('Replacing {number_to_replace} {old_rack_name} with {new_rack_name}')
		slot_list = self.rack_assignments[old_rack_name][:number_to_replace]
		if self.print_comments:
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
		if self.ignore_slots != []:
			slots = [slot for slot in slots if slot not in self.ignore_slots]
			if self.print_comments:
				self.ctx.comment(f'Ignoring slots {self.ignore_slots} for refill')
			if self.debug:
				print(f'Ignoring slots {self.ignore_slots} for refill')
		if self.print_comments:
			self.ctx.comment(f'Refilling tips of {name} on {slots}')
		if self.debug:
			print(f'Refilling tips of {name} on {slots}')
		self.clear_old(name)
		
		self.load_tipracks(name,slots)

	def waste_tips(self, slots):
		'''Move tipboxes to waste, this is done automatically when a all types of a tip are used or when refill_all=True for tip pickup but can be used to manually move tips from any slots to waste. \
		If called manually, make sure to use clear_old on the slots after to remove it from internal data \
		slots = list of slots to move to waste, if str or labware will be converted to list. Labware should be passed only if it is a tiprack adapter'''
		if self.print_comments:
			self.ctx.comment(f'Wasting tips on slots {slots}: Using gripper : {self.use_gripper}')
		if self.debug:
			print(f'Wasting tips on slots {slots}: Using gripper : {self.use_gripper}')
		if type(slots) == str or type(slots) == protocol_api.Labware:
			slots = [slots]
		if self.use_chute and self.use_gripper:
			for slot in slots:
				if slot in self.ignore_slots:
					if self.debug:
						print(f'Ignoring slot {slot} for waste tips')
					continue
				self.ctx.move_labware(self.ctx.deck[slot], self.waste,use_gripper=self.use_gripper)
		else:
			for slot in slots:
				if slot in self.ignore_slots:
					if self.debug:
						print(f'Ignoring slot {slot} for waste tips')
					continue
				self.ctx.move_labware(self.ctx.deck[slot], protocol_api.OFF_DECK)

	def assign_tipracks(self, pipette : int | str | protocol_api.InstrumentContext, name : str):
		'''Assign tipracks to pipette, this is done automatically when loading tips but can be used to reassign if needed.\
		Instead of pip.tip_racks = [tipracks], use trackerObj.assign_tipracks(1,opentrons_flex_96_filtertip_50ul).\
		pipette = 1 or 2, corresponding to which order you loaded them in
		name = tiprack load name as str'''
		if self.print_comments:
			self.ctx.comment(f'Reassigning tipracks of {pipette} to {name}')
		if self.debug:
			print(f'Reassigning tipracks of {pipette} to {name}')
		pip = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
		if pip == None:
			raise ValueError(f"Invalid pipette number {pipette}, must be 1 or 2")
		pip.tip_racks = self.tipracks[name]

	def clear_old(self,name : str ,slots_to_clear : None | list = None,save_tips = True, waste_expansion : bool = False):
		'''Remove old tipracks from internal data to replace with new tipracks in another function. This should generally only be used internally.\
		Only use if you are sure you want to remove the tipracks from the internal data without moving them off deck physically. Keeps protocol from trying to move labware not on the deck anymore
		name = Tiprack load name
		slots_to_clear = List of slots to clear, if None will clear all tipracks of that type'''
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
			self.ctx.pause(f'Please remove all {name} from {slots_message}')
			toss_tips = False
			toss_location = protocol_api.OFF_DECK
		if slots_to_clear == None:
			if name in self.rack_assignments.keys():
				for rack in self.tipracks.get(name,[]):
					self.ctx._core.move_labware(
						labware_core=rack._core,
						new_location=toss_location,
						use_gripper=toss_tips,
						pause_for_manual_move=False,
						pick_up_offset=(0.0,0.0,0.0),
						drop_offset=(0.0,0.0,0.0))
				self.tipracks[name] = []
				for rack in self.ex_racks.get(name,[]):
					self.ctx._core.move_labware(
						labware_core=rack._core,
						new_location=toss_location,
						use_gripper=toss_tips,
						pause_for_manual_move=False,
						pick_up_offset=(0.0,0.0,0.0),
						drop_offset=(0.0,0.0,0.0))
				self.ex_racks[name] = []
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


SPECIES_CONFIG = {
		'Strawberry' : {'inputPlate' : 'opentrons_96_wellplate_200ul_pcr_full_skirt', 'BufferVolumeA' : 50, 'BufferVolumeB' : 10, 'ExtractionTransfer' : 40, 'BufferVolumeC' : 48, 'DNATransferVolume' : 2},
		'PLACEHOLDER' : {'inputPlate' : 'nest_96_wellplate_2ml_deep', 'BufferVolumeA' : 50, 'BufferVolumeB' : 10, 'ExtractionTransfer' : 40, 'BufferVolumeC' : 48, 'DNATransferVolume' : 2},
	}
MIX_REPITITIONS = 3 #Number of times to mix after adding reagents to wells, incease for more mixing, decrease for less mixing
BEAD_INCUBATION_TIME = 5 #Time in minutes to incubate beads after adding to wells, increase for more incubation, decrease for less incubation
BEAD_DRYING_TIME = 1 #Time in minutes to dry beads after removing supernatant, increase for more drying, decrease for less drying
BEAD_SETTLING_TIME = 5 #Time in minutes to allow beads to collect on the magnetic deck
USE_ONE_TIP_POOLING = True
NORMALIZATION_BEAD_VOLUME = 9
TOTAL_POOLING_VOLUME = 45 #Total volume of pooled samples transferred to the pooled working plate
NORMALIZATION_MASTERMIX_VOLUME = 15 #Volume of normalization mastermix to add to each well in the pooled working plate
REMOVE_SUPERNATANT_HEIGHT = 1.2

#RUNTIME PARAMTERS
def add_parameters(parameters : protocol_api.Parameters ):
	parameters.add_int(variable_name='num_plates',
						display_name='Number of Columns',
						description='How many Columns to clean',
						default=4,
						minimum=1,
						maximum=4,)
	parameters.add_bool(variable_name="dry_run",
						display_name="Dry Run",
						description="Off for real run with samples, On to shorten incubation steps / thermocycler steps",
						default=False)

def run(ctx : protocol_api.ProtocolContext):

	######################
	# RUNTIME PARAMETERS #
	######################

	params = ctx.params

	number_of_columns = params.num_plates
	dry_run = params.dry_run


	########################
	# HARDWARE AND LABWARE #
	########################

	#HARDWARE
	hs_adapter_type = 'opentrons_96_pcr_adapter'
	tm_adapter_type = 'opentrons_96_deep_well_temp_mod_adapter'
	heater_shaker = ctx.load_module(module_name='heaterShakerModuleV1',location='D1')
	hs_adpater = heater_shaker.load_adapter(hs_adapter_type)
	temp_module= ctx.load_module(module_name='temperature module gen2',location='C1')
	tm_adapter = temp_module.load_adapter(tm_adapter_type)
	reagent_plate = tm_adapter.load_labware('nest_96_wellplate_2ml_deep')
	waste = ctx.load_waste_chute()
	magblock = ctx.load_module(module_name='magneticBlockV1', location='D2')

	#PIPETTES
	m50 = ctx.load_instrument('flex_8channel_50', mount='left')
	m1000 = ctx.load_instrument('flex_8channel_1000', mount='right')
	
	#Labware
	sample_plate = ctx.load_labware('opentrons_96_wellplate_200ul_pcr_full_skirt','D4','Final Eluted Sample Plate') 
	pooled_plate = hs_adpater.load_labware('opentrons_96_wellplate_200ul_pcr_full_skirt', 'Pooled Reaction Plate')
	reagent_reservior = ctx.load_labware('nest_12_reservoir_15ml', 'C2', 'Reagent Reservoir')
	#plates_in_queue = [ctx.load_labware('opentrons_96_wellplate_200ul_pcr_full_skirt', slot) for slot in ['A1','A2']]
	#Want to go to 8 columns, move reagents into deepwell plate
	#mix beads before adding
	#Lrave most sample in the beads, pools directly into the 2mL tubes with 65


	#TIPRACKS
	TipManager = TipTracker(ctx,m50,m1000,waste,True)
	TipManager.add_expansion_slots(['A4','B4','C4'])
	TIPS50 = 'opentrons_flex_96_tiprack_50ul'
	TIPS200 = 'opentrons_flex_96_tiprack_200ul'
	TIPS1000 = 'opentrons_flex_96_tiprack_1000ul'

	#AM 08/28/25
	TipManager.open_slot = 'C4' #First slot to put empty tips
	TipManager.carousel_tips = True #Turn on carouseling empty boxes
	TipManager.use_chute = False #Turn off wasting tips

	#tips_columns_needed =  num_plates + (num_plates - 1) * 12 + number_of_columns # Amount of tips needed, assumes only using one tip size, not sure if realisitic

	slots_50 = ['B2'] # 8 columns per tiprack, so we need at least one tiprack for every 8 columns
	slots_200 = ['A1','A2','A3','B1'][:number_of_columns]  # Currently do not need larger tip slots, but could for some species, uncomment the lines below and add to TipManager.add_starting_tipracks if needed
	#slots_1000 = ['B2']

	TipManager.add_starting_tipracks(TIPS50,slots_50,TIPS200,slots_200, ) #TIPS1000,slots_1000)

	#################################
	# LIQUIDS AND WELL ACCESSIONING #
	#################################
	bead_liquid = ctx.define_liquid('Normalization Beads', 'Normalization beads for DNA binding', display_color='#A0522D')
	waste_liquid = ctx.define_liquid('Liquid Waste','Liquid Waste made by protocol',display_color='#D3D3D3')
	final_sample_liquids = ctx.define_liquid(f'Final Sample', 'Final Sample from Final Amp',display_color='#ffb3ff')
	normalization_wash_liquid = ctx.define_liquid('Normalization Wash','Wash buffer for normalization cleanup',display_color='#228B22')
	elution_liquid = ctx.define_liquid('Elution Solution', 'Elution solution for after normalization cleanup', display_color='#87CEFA')
	normalization_reagent_liquid = ctx.define_liquid('Normalization Reagent', 'Reagent for normalization, supplied by user', display_color='#FF69B4')


	normalization_wash_buffer = reagent_plate['A1']
	normalization_elution_buffer = reagent_plate['A2']
	normalization_reagent = reagent_plate['A3']
	normalization_beads = reagent_plate['A4']
	liquid_waste = reagent_reservior.rows()[0][-2:]

	# Normalization Wash Buffer
	for well in reagent_plate.columns()[0]:
		well.load_liquid(normalization_wash_liquid,volume=224*number_of_columns * 1.2)
	for col in pooled_plate.columns()[:number_of_columns]:
		for well in col:
			well.load_liquid(final_sample_liquids,volume=78)
	# Elution Buffer
	for well in reagent_plate.columns()[1]:
		well.load_liquid(elution_liquid,volume=number_of_columns*1.2*75)
	# Normalization Reagent
	for well in reagent_plate.columns()[2]:
		well.load_liquid(normalization_reagent_liquid,volume=number_of_columns*1.4*15) #84 for 4 plates
	# Beads
	for well in reagent_plate.columns()[3]: 
		well.load_liquid(bead_liquid,volume=number_of_columns*9*1.4) #50 for 4 plates
	for col in reagent_plate.columns()[-2:]:
		for well in col:
			well.load_liquid(waste_liquid,volume=0)
		





	########################
	#   CUSTOM FUNCTIONS   #
	########################
		
	def add_wash_buffer(
			wells_to_add : list[protocol_api.Well],
			source_reagent : protocol_api.Well,
			volume_to_add : float | int,
			pipette : protocol_api.InstrumentContext,
			hover_pipette : bool = True,
			mix_after : bool = True,
			mix_volume : float | int | None = None):
	
		mix_volume = volume_to_add if mix_volume == None else mix_volume

		flow_rates = [volume_to_add / (pipette.flow_rate.aspirate * 2),0.5] # [0] = aspirate, [1] = dispense
		for well in wells_to_add:
			dispense_location = well.top(-4) if hover_pipette else well.bottom(2).move(types.Point(x=well.diameter/4,y=0,z=0)) #try to dispense on the side of the well
			
			if not pipette.has_tip:
				TipManager.pick_up(pipette)

			pipette.aspirate(
				volume=volume_to_add,
				location=source_reagent.bottom(2),
				rate=flow_rates[1])
			pipette.dispense(
				volume=volume_to_add,
				location=dispense_location,
				rate=flow_rates[1],
				push_out=0)
			
			
		if mix_after:
			for well in wells_to_add:
				if not pipette.has_tip:
					TipManager.pick_up(pipette)
				mix_locations = [well.bottom(1.5),well.bottom(5)] #[0] = aspirate, [1] = dispense to move around liquid
				for i in range(3): # 3 mix reps after adding wash buffer
					pipette.aspirate(
						volume=mix_volume,
						location=mix_locations[0],
						rate=flow_rates[1])
					pipette.dispense(
						volume=mix_volume,
						location=mix_locations[1],
						rate=flow_rates[1],
						push_out=0)
				pipette.blow_out(location=well.top(-3))
				pipette.touch_tip(speed=40)
				TipManager.drop_tip(pipette,return_tip=dry_run)
	
	def add_to_plate(
			wells_to_add : list[protocol_api.Well],
			source_reagent : protocol_api.Well,
			volume_to_add : float | int,
			pipette : protocol_api.InstrumentContext,
			hover_pipette : bool = True,
			mix_after : bool = False,
			dispense_rate : float | int = 0.5,
			mix_volume : float | int | None = None):
	
		mix_volume = volume_to_add if mix_volume == None else mix_volume
		if mix_after and hover_pipette:
			raise('Cannot mix after adding to wells with hover pipette, please set hover_pipette to False')
		flow_rates = [volume_to_add / (pipette.flow_rate.aspirate * 2),0.5] # [0] = aspirate, [1] = dispense
		for well in wells_to_add:
			dispense_location = well.top(-4) if hover_pipette else well.bottom(2).move(types.Point(x=well.diameter/4,y=0,z=0)) #try to dispense on the side of the well
			# mix_locations = [well.bottom(1.5),well.bottom(5)] #[0] = aspirate, [1] = dispense to move around liquid
			
			if not pipette.has_tip:
				TipManager.pick_up(pipette)

			pipette.aspirate(
				volume=volume_to_add,
				location=source_reagent.bottom(0.5),
				rate=flow_rates[0])
			pipette.dispense(
				volume=volume_to_add,
				location=dispense_location,
				rate=dispense_rate) # HW eDIT 16Jul25 - switche dto dispense_rate instead of flow_rates[1]
			
			if mix_after:
				for i in range(40): # Mix reps after adding elution buffer (all other call for False rn). HW Edit 16Jul25
					pipette.aspirate(
						volume=mix_volume,
						location=well.bottom(1.5), # EB mix aspirate height
						rate=flow_rates[1])
					pipette.dispense(
						volume=mix_volume,
						location=well.bottom(6), # EB mix dispense height
						push_out=0,
						rate=flow_rates[1])
				pipette.blow_out(location=well.top(-4))
				pipette.touch_tip(speed=40)
			if not hover_pipette or well == wells_to_add[-1]:
				TipManager.drop_tip(pipette,return_tip=dry_run)


	def remove_supernatant(samples_to_remove : list[protocol_api.Well],
						 pipette : protocol_api.InstrumentContext,
						 waste_bin : protocol_api.Well) -> None:

		
		for well in samples_to_remove:
			if not pipette.has_tip:
				TipManager.pick_up(pipette)
			pipette.aspirate(200, well.bottom(REMOVE_SUPERNATANT_HEIGHT), rate=0.10)
			pipette.dispense(200, waste_bin.top(-1.5), rate=0.1) # Lowered rate from 1 to 0.2 . HW Edit 20Jun25
			pipette.touch_tip(waste_bin, v_offset=-3, speed=40, radius=0.7) # Modified to only touch sides. HE Edit 20Aug25. HW Edit 30Jul25. - touch tip to avoid bubble formation on tips.
			# pipette.blow_out()  # HW Edit 30Jul25 - avoid additional bubble formation on tips
			TipManager.drop_tip(pipette, return_tip=dry_run)

	def remove_remaining_ethanol(samples_to_remove : list[protocol_api.Well], #Added function to remove excess ethanol with p50 tips. HW Edit 19Jun25
						 pipette : protocol_api.InstrumentContext,
						 waste_bin : protocol_api.Well) -> None:

		for well in samples_to_remove:
			if not pipette.has_tip:
				TipManager.pick_up(pipette)
			pipette.aspirate(25, well.bottom(0.5), rate=0.10) #Increase well.bottom to raise tip, decrease to lower tip into well. 
			pipette.dispense(25, waste_bin.top(-1.5), rate=0.5)
			pipette.touch_tip(waste_bin, v_offset=-3, speed=40) #HW Edit 15Jul25 - added touch tip after ethanol removal to avoid ethanol droplets moving across deck. 
			# pipette.blow_out() # HW Edit 30Jul25 - commented to avoid additional bubble formation on tips
			TipManager.drop_tip(pipette, return_tip=dry_run)

	######################
	#   PROTOCOL STEPS   #
	######################
	TipManager.assign_tipracks(m1000,TIPS200)
	TipManager.assign_tipracks(m50,TIPS50)
	heater_shaker.close_labware_latch()
    
	#Adding Norm Reagent and Mixing
	ctx.comment('Adding Normalization Reagent to Pooled Plate')
	add_to_plate(
		wells_to_add=pooled_plate.rows()[0][:number_of_columns],
		source_reagent=normalization_reagent,
		volume_to_add=NORMALIZATION_MASTERMIX_VOLUME,
		pipette=m50,
		hover_pipette=False,
		mix_after=False) # Removed mixing after so I could add mix step below with new tips. HW Edit 16Jul25
	
	# Mixing Normalization Reagent with 200uL tips. HW Edit 16Jul25
	for well in pooled_plate.rows()[0][:number_of_columns]:
		if not m1000.has_tip:
			TipManager.pick_up(m1000)
		m1000.mix(15, 60, well.bottom(3), rate=0.5) # 15 reps, 60uL, 3mm height, rate. HW Edit 30Jul25
		m1000.blow_out(well.top(-3))
		m1000.touch_tip(speed=40)
		TipManager.drop_tip(m1000, return_tip=dry_run)

	# Incubate 5 mins
	if not dry_run:
		ctx.delay(minutes=3,msg='Incubating Normalization Reagent')
	
	# Add Normalization Beads
	ctx.comment('Adding Normalization Beads to Pooled Plate')
	add_to_plate(
		wells_to_add=pooled_plate.rows()[0][:number_of_columns],
		source_reagent=normalization_beads,
		volume_to_add=NORMALIZATION_BEAD_VOLUME,
		pipette=m50,
		hover_pipette=False,
		mix_after=False) # Removed mixing after so I could add mix step below with new tips. HW Edit 16Jul25

    # Mixing Beads with 200uL tips # HW Edit 16Jul25
	for well in pooled_plate.rows()[0][:number_of_columns]:
		if not m1000.has_tip:
			TipManager.pick_up(m1000)
		m1000.mix(15, 65, well.bottom(3))  # 15 reps, 65uL, 3mm height
		m1000.blow_out(well.top(-3))
		m1000.touch_tip(speed=40)
		TipManager.drop_tip(m1000, return_tip=dry_run)

	# Let Beads bind DNA
	if not dry_run:
		ctx.delay(minutes=BEAD_INCUBATION_TIME,msg='Inbuating Normalization Beads')
	
	# Washing Loop
	for i in range(2):
		
		# Move plate to magnet and let beads settle
		if i == 0:
			heater_shaker.open_labware_latch()
			ctx.move_labware(pooled_plate, magblock, use_gripper=True)
			if not dry_run:
				ctx.delay(minutes=BEAD_SETTLING_TIME,msg='Letting Beads settle on magnet')

			# Remove Supernatant
			ctx.comment(f'Removing Supernatant from Pooled Plate')
			remove_supernatant(
				samples_to_remove=pooled_plate.rows()[0][:number_of_columns],
				pipette=m1000,
				waste_bin=liquid_waste[0] if i == 0 else liquid_waste[1])
		
		# Add Wash 
		add_wash_buffer(
			wells_to_add=pooled_plate.rows()[0][:number_of_columns],
			source_reagent=normalization_wash_buffer,
			volume_to_add=112,
			pipette=m1000,
			hover_pipette=True,
			mix_after=True,
			mix_volume=100)

		# Remove Wash
		remove_supernatant(
			samples_to_remove=pooled_plate.rows()[0][:number_of_columns],
			pipette=m1000,
			waste_bin=liquid_waste[0] if i == 0 else liquid_waste[1])
	    
		if i == 1: # HW Edit 16Jul25. - Added function call.
			# Remove extra wash buffer after 2nd wash
			remove_remaining_ethanol(
				samples_to_remove=pooled_plate.rows()[0][:number_of_columns],
				pipette=m50,
				waste_bin=liquid_waste[0] if i == 0 else liquid_waste[1])
			
	
	# Dry Beads (only after 2nd round of cleanup)
	heater_shaker.set_target_temperature(37)
	ctx.move_labware(pooled_plate,hs_adpater,use_gripper=True)
	heater_shaker.close_labware_latch()

	for x,column in enumerate(pooled_plate.rows()[0][:number_of_columns]):
		
		# Add Elution Buffer
		add_to_plate(
			wells_to_add=[column],
			source_reagent=normalization_elution_buffer,
			volume_to_add=78,
			pipette=m1000,
			hover_pipette=False,
			mix_after=True,
			mix_volume=50)

	# Incubate EB with beads at 32*C	
	if not dry_run:
		ctx.delay(minutes=BEAD_INCUBATION_TIME,msg='Letting beads incubate') 
	heater_shaker.open_labware_latch()
	heater_shaker.deactivate_heater() # Added step to turn off heat so that the eluates aren't heated. HW Edit 23Jun25
	
	# Move to Magnet
	ctx.move_labware(pooled_plate, magblock, use_gripper=True)
	ctx.move_labware(sample_plate,hs_adpater,use_gripper=True)
	heater_shaker.close_labware_latch()
	if not dry_run:
		ctx.delay(minutes=2.0) #HW Edit 30Jul25 - replaced Bead Settling Time with 2 minutes to change this time but not settling time when washing. 

    # Remove Eluates to Final Eluate plate
	for x,column in enumerate(pooled_plate.rows()[0][:number_of_columns]):
		add_to_plate(
			wells_to_add=[sample_plate.rows()[0][x]],
			source_reagent=column,
			volume_to_add=65,
			pipette=m1000,
			hover_pipette=False,
			mix_after=False,
			dispense_rate=0.1) # HW Edit 16Jul25 - changed dispense rate to 0.1 for slower dispense

	heater_shaker.open_labware_latch()
